"""Run workers: pull jobs from the store's queue and drive runs.

    python -m alm_agents.worker        # a worker process (also runs inside the API)

A job moves one run forward to its next pause or its end:

* ``start``     - begin a run over the work items in the payload
* ``resume``    - continue a run parked at the approval gate with a decision
* ``reconcile`` - begin a run over the whole active queue (the sweep)

What makes several workers on several machines safe together:

* **One worker per run.** The store never hands a thread to two workers (see
  ``claim_job``), and a worker renews its claim while it works.
* **A dead worker hands over, it does not stop.** Its lease expires, another
  worker claims the job, and the run continues from its last checkpoint. The
  idempotency ledger reports any write that already happened as a replay.
* **Approval does not block a worker.** A run that reaches the gate is parked
  (``awaiting_approval``) and the job ends; the decision arrives as a new job.
* **Stop works from anywhere.** A stop request is a row in the store; the
  worker holding the run reads it within a second and the run ends after its
  current step - a write in progress always finishes.
* **One scheduler.** The worker holding the ``scheduler`` lease enqueues the
  reconcile sweep, once per interval, whatever the number of workers.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import tempfile
import time
import uuid
from pathlib import Path

from alm_core.errors import ConfigError
from alm_core.logging import get_logger, scrub_secrets

log = get_logger("alm.worker")

START, RESUME, RECONCILE = "start", "resume", "reconcile"
RUN_KINDS = (START, RESUME, RECONCILE)
STOP_POLL_SECONDS = 1.0
TRACE_FLUSH_SECONDS = 1.0
SCHEDULER_LEASE = "scheduler"


# ------------------------------------------------------------ submitting work

async def submit_run(store, *, thread_id: str, work_item_ids: list[str] | None,
                     mode: str, requested_by: str, trigger: str,
                     operator_request: str = "", environment: str = "") -> int | None:
    """Register a run and queue its start. Returns the job id, or None when the
    same run is already queued or running (a redelivered trigger)."""
    if await store.get_run(thread_id) and await _unfinished(store, thread_id):
        return None
    await store.clear_stop(thread_id)
    await store.upsert_run(thread_id, status="queued", mode=mode,
                           scope=list(work_item_ids or []), requested_by=requested_by,
                           trigger=trigger, operator_request=operator_request,
                           environment=environment, error="", report=None)
    return await store.enqueue_job(START, thread_id, {
        "work_item_ids": list(work_item_ids or []), "trigger": trigger,
        "operator_request": operator_request, "mode": mode}, dedupe=True)


async def submit_decision(store, thread_id: str, decision) -> int | None:
    """Queue the resume of a run parked at the approval gate."""
    payload = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else decision
    await store.upsert_run(thread_id, status="queued")
    return await store.enqueue_job(RESUME, thread_id, {"decision": payload})


async def request_stop(store, thread_id: str, by: str) -> str:
    """Ask a run to stop. Returns its status afterwards.

    A running run ends after its current step. A queued run never starts. A
    run parked at the approval gate is resumed only to record that it stopped.
    """
    run = await store.get_run(thread_id)
    if run is None:
        raise LookupError(thread_id)
    if run["status"] not in ("queued", "running", "awaiting_approval", "stopping"):
        return run["status"]
    await store.request_stop(thread_id, by)
    if run["status"] == "awaiting_approval":
        await store.enqueue_job(RESUME, thread_id, {"decision": None}, dedupe=True)
    await store.upsert_run(thread_id, status="stopping")
    return "stopping"


async def _unfinished(store, thread_id: str) -> bool:
    run = await store.get_run(thread_id)
    return bool(run and run["status"] in ("queued", "running", "stopping"))


# -------------------------------------------------------------------- worker

class Worker:
    """Claims jobs and drives runs, up to ``concurrency`` at once."""

    def __init__(self, services, *, worker_id: str = "", concurrency: int | None = None,
                 lease_seconds: float | None = None, poll_seconds: float | None = None,
                 max_attempts: int | None = None, trace_dir: str | None = None,
                 backend=None, on_event=None):
        settings = services.settings
        self.services = services
        self.store = services.store
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = max(1, concurrency if concurrency is not None
                               else getattr(settings, "worker_concurrency", 1) or 1)
        self.lease = float(lease_seconds or getattr(settings, "job_lease_seconds", 120))
        self.poll = float(poll_seconds or getattr(settings, "worker_poll_seconds", 2.0))
        self.max_attempts = int(max_attempts or getattr(settings, "job_max_attempts", 5))
        self.reconcile_minutes = float(getattr(settings, "reconcile_interval_minutes", 15))
        base = trace_dir if trace_dir is not None else (
            getattr(settings, "trace_dir", "") or tempfile.gettempdir())
        self.trace_dir = Path(base) / "alm"
        self.backend = backend          # tests and the sandbox inject one
        self.on_event = on_event        # extra progress hook (the web console)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ the loop

    async def run_forever(self, stopping: asyncio.Event) -> None:
        """Claim and run jobs until ``stopping`` is set, then let running jobs
        finish for a while. Anything still running is taken over elsewhere."""
        log.info("worker_started", worker=self.worker_id, concurrency=self.concurrency)
        while not stopping.is_set():
            try:
                await self.schedule()
                claimed = await self.fill()
            except Exception:  # noqa: BLE001 - the loop must outlive a bad poll
                log.exception("worker_poll_failed", worker=self.worker_id)
                claimed = False
            if not claimed:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stopping.wait(), self.poll)
        if self._tasks:
            log.info("worker_draining", worker=self.worker_id, running=len(self._tasks))
            await asyncio.wait(self._tasks, timeout=max(5.0, self.lease / 4))
        log.info("worker_stopped", worker=self.worker_id)

    async def fill(self) -> bool:
        """Claim jobs while there is a free slot. True if any was claimed."""
        claimed = False
        while len(self._tasks) < self.concurrency:
            job = await self.store.claim_job(self.worker_id, self.lease,
                                             max_attempts=self.max_attempts, kinds=RUN_KINDS)
            if job is None:
                break
            claimed = True
            task = asyncio.create_task(self.handle(job), name=f"alm-job-{job['id']}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return claimed

    async def drain(self) -> None:
        """Run every ready job to completion - for tests and one-shot use."""
        while True:
            await self.fill()
            if not self._tasks:
                return
            await asyncio.wait(self._tasks)

    async def schedule(self) -> None:
        """The scheduler lease holder queues one reconcile sweep per interval."""
        if self.reconcile_minutes <= 0:
            return
        if not await self.store.try_lease(SCHEDULER_LEASE, self.worker_id, self.lease):
            return
        interval = self.reconcile_minutes * 60
        bucket = int(time.time() // interval)
        thread_id = f"reconcile-{bucket}"
        if await self.store.get_run(thread_id) is not None:
            return
        settings = self.services.settings
        await submit_run(self.store, thread_id=thread_id, work_item_ids=None,
                         mode="dry" if settings.shadow_mode else "commit",
                         requested_by="scheduler", trigger=RECONCILE,
                         environment=getattr(settings, "environment", ""))
        log.info("reconcile_scheduled", thread_id=thread_id, worker=self.worker_id)

    # -------------------------------------------------------------- one job

    async def handle(self, job: dict) -> None:
        from .control import RunControl
        from .trace import open_run_trace

        thread_id = job["thread_id"]
        control = RunControl(thread_id=thread_id)
        trace = open_run_trace(self.trace_dir, thread_id, settings=self.services.settings)
        pending: list[dict] = []
        trace.listeners.append(pending.append)
        keeper = asyncio.create_task(self._keep(job, control, pending, thread_id))
        outcome = {"ok": True, "error": ""}
        try:
            with trace.bound():
                trace.write({"service": "run", "kind": f"job_{job['kind']}",
                             "worker": self.worker_id, "job": job["id"],
                             "attempt": job["attempts"]})
                await self._drive(job, control, trace)
        except Exception as err:  # noqa: BLE001 - recorded on the run and the job
            message = scrub_secrets(f"{type(err).__name__}: {err}")[:600]
            log.exception("job_failed", job=job["id"], thread_id=thread_id)
            permanent = isinstance(err, ConfigError | LookupError | ValueError)
            outcome = {"ok": False, "error": message, "permanent": permanent}
            final = permanent or job["attempts"] >= self.max_attempts
            await self.store.upsert_run(thread_id, status="failed" if final else "queued",
                                        error=message)
        finally:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
            await self._flush(thread_id, pending)
            trace.close()
        await self.store.finish_job(
            job["id"], self.worker_id, ok=outcome["ok"], error=outcome["error"],
            max_attempts=job["attempts"] if outcome.get("permanent") else self.max_attempts)

    async def _keep(self, job: dict, control, pending: list, thread_id: str) -> None:
        """While a job runs: renew its lease, watch for a stop, ship its trace."""
        last_renew = time.monotonic()
        while True:
            await asyncio.sleep(min(STOP_POLL_SECONDS, TRACE_FLUSH_SECONDS))
            stop = await self.store.stop_request(thread_id)
            if stop and not control.stop_requested():
                control.request_stop(stop["by"])
            await self._flush(thread_id, pending)
            if time.monotonic() - last_renew >= self.lease / 3:
                if not await self.store.extend_job(job["id"], self.worker_id, self.lease):
                    log.warning("job_lease_lost", job=job["id"], thread_id=thread_id)
                last_renew = time.monotonic()

    async def _flush(self, thread_id: str, pending: list) -> None:
        if not pending:
            return
        batch, pending[:] = list(pending), []
        try:
            await self.store.record_trace(thread_id, batch)
        except Exception:  # noqa: BLE001 - the local file still has them
            log.exception("trace_flush_failed", thread_id=thread_id)

    async def _drive(self, job: dict, control, trace) -> None:
        from .graph import resume_run, run_config, run_session, start_run
        from .runner import build_report, pending_interrupt

        thread_id = job["thread_id"]
        run = await self.store.get_run(thread_id) or {}
        stop = await self.store.stop_request(thread_id)
        if stop:
            control.request_stop(stop["by"])
        mode = run.get("mode") or job["payload"].get("mode") or ""

        def on_event(kind: str, data: dict) -> None:
            trace.event(kind, data)
            if self.on_event is not None:
                self.on_event(thread_id, kind, data)

        graph, ctx = run_session(self.services, control=control, on_event=on_event,
                                 backend=self.backend, mode=mode,
                                 shots_dir=str(self.trace_dir / "evidence" / thread_id))
        config = run_config(thread_id)
        snapshot = await graph.aget_state(config)
        started = time.monotonic()

        if job["kind"] in (START, RECONCILE):
            if stop and not snapshot.values:
                await self.store.upsert_run(thread_id, status="stopped",
                                            error=f"stopped by {stop['by']} before it started")
                return
            await self.store.upsert_run(thread_id, status="running")
            if not snapshot.values:
                payload = job["payload"]
                await start_run(graph, ctx, thread_id=thread_id,
                                work_item_ids=payload.get("work_item_ids") or None,
                                trigger=payload.get("trigger", job["kind"]),
                                operator_request=payload.get("operator_request", ""))
                await self.store.upsert_run(thread_id, run_id=ctx.run_id)
        else:  # RESUME
            if not snapshot.values:
                raise LookupError(f"no saved run {thread_id} to resume")
            await self.store.upsert_run(thread_id, status="running")
            card = await pending_interrupt(graph, thread_id)
            if card is not None:
                ctx.run_id = snapshot.values.get("run_id", "")
                ctx.thread_id = thread_id
                await resume_run(graph, ctx, thread_id=thread_id,
                                 decision=self._decision(job, card, control, thread_id))

        # Whatever the job, a run that is neither parked nor finished continues
        # from its last checkpoint: this is how a dead worker's run is taken
        # over. The ledger turns any write that already happened into a replay.
        snapshot = await graph.aget_state(config)
        if snapshot.values and snapshot.next and                 await pending_interrupt(graph, thread_id) is None:
            ctx.run_id = snapshot.values.get("run_id", "") or ctx.run_id
            ctx.thread_id = thread_id
            trace.write({"service": "run", "kind": "continued",
                         "from": list(snapshot.next)})
            await graph.ainvoke(None, config=config)

        # A dry run cannot write, so its card is a preview of what a writing run
        # would ask: shown in the trace, then the plan carries on. Nobody votes.
        for _ in range(3):
            card = await pending_interrupt(graph, thread_id)
            if card is None or not ctx.shadow:
                break
            from alm_core.models import ApprovalDecision

            shown = [str(i.get("userid")) for i in card.get("items", []) if i.get("userid")]
            trace.write({"service": "approval", "kind": "preview", "users": shown,
                         "reason": card.get("reason", "")})
            ctx.run_id = ctx.run_id or snapshot.values.get("run_id", "")
            ctx.thread_id = thread_id
            await resume_run(graph, ctx, thread_id=thread_id, decision=ApprovalDecision(
                thread_id=thread_id, approved=True, approver="dry-run:preview",
                plan_hash=card.get("plan_hash", ""), approved_userids=shown,
                comment="dry run: preview of the card a writing run asks for"))

        if await pending_interrupt(graph, thread_id) is not None:
            await self.store.upsert_run(thread_id, status="awaiting_approval")
            trace.write({"service": "run", "kind": "parked", "reason": "awaiting approval"})
            return
        report = await build_report(graph, ctx, thread_id=thread_id, approvals=None,
                                    wall_seconds=time.monotonic() - started)
        report.pop("audit_events", None)
        reason = report.get("halt_reason", "") or ""
        status = "stopped" if report["halted"] and reason.startswith("stopped") else "done"
        await self.store.upsert_run(thread_id, status=status, report=report,
                                    run_id=report.get("run_id") or ctx.run_id)
        trace.write({"service": "run", "kind": "finished", "status": status,
                     "halted": report["halted"], "halt_reason": reason,
                     "metrics": report.get("metrics") or {}})


    @staticmethod
    def _decision(job: dict, card: dict, control, thread_id: str):
        """The decision a resume job carries - or, for a stop on a parked run,
        a decline whose effect is that the gate sees the stop and halts."""
        from alm_core.models import ApprovalDecision

        decision = job["payload"].get("decision")
        if decision is not None:
            return ApprovalDecision.model_validate(decision)
        return ApprovalDecision(thread_id=thread_id, approved=False,
                                approver=getattr(control, "by", "") or "stop",
                                plan_hash=card.get("plan_hash", ""), comment="run stopped")


# ---------------------------------------------------------------------- main

async def serve(settings=None, *, stopping: asyncio.Event | None = None) -> None:
    """Build the shared services and run a worker until told to stop."""
    from alm_api.notify import make_notifier
    from alm_core.config import get_settings
    from alm_core.credentials import build_resolver

    from .graph import build_services

    settings = settings or get_settings()
    stopping = stopping or asyncio.Event()
    notifier = make_notifier(settings, build_resolver(settings))
    async with build_services(settings, notifier=notifier) as services:
        await Worker(services).run_forever(stopping)


def main() -> int:
    import signal

    from alm_core.logging import configure

    configure()
    stopping = asyncio.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, AttributeError, ValueError):
                loop.add_signal_handler(sig, stopping.set)
        await serve(stopping=stopping)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
