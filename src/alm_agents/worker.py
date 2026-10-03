"""Run workers: pull jobs from the store's queue and drive runs.

    python -m alm_agents.worker                  # a worker process (also runs inside the API)
    python -m alm_agents.worker synthetic 1001   # queue a dry run of 1001: a scheduled check

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
from datetime import datetime, timezone
from pathlib import Path

from alm_core import telemetry
from alm_core.errors import ConfigError
from alm_core.logging import get_logger, scrub_secrets

log = get_logger("alm.worker")

START, RESUME, RECONCILE = "start", "resume", "reconcile"
FALLBACK = "fallback"                # the trigger of a run re-done without a model
MODEL_DOWN = "the agent model is unavailable"
RUN_KINDS = (START, RESUME, RECONCILE)
STOP_POLL_SECONDS = 1.0
TRACE_FLUSH_SECONDS = 1.0
SCHEDULER_LEASE = "scheduler"


# ------------------------------------------------------------ submitting work

async def submit_run(store, *, thread_id: str, work_item_ids: list[str] | None,
                     mode: str, requested_by: str, trigger: str,
                     operator_request: str = "", environment: str = "",
                     orchestration: str = "") -> int | None:
    """Register a run and queue its start. Returns the job id, or None when the
    same run is already queued or running (a redelivered trigger).

    ``orchestration`` pins the run to one (the fallback run is deterministic);
    empty means the deployment's.
    """
    if await store.get_run(thread_id) and await _unfinished(store, thread_id):
        return None
    await store.clear_stop(thread_id)
    await store.upsert_run(thread_id, status="queued", mode=mode,
                           scope=list(work_item_ids or []), requested_by=requested_by,
                           trigger=trigger, operator_request=operator_request,
                           environment=environment, error="", report=None, version="")
    payload = {"work_item_ids": list(work_item_ids or []), "trigger": trigger,
               "operator_request": operator_request, "mode": mode}
    if orchestration:
        payload["orchestration"] = orchestration
    return await store.enqueue_job(START, thread_id, payload, dedupe=True)


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
        self._depth_read = 0.0

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
        await self._read_depth()
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

    async def _read_depth(self) -> None:
        """Refresh the queue-depth gauge, at most every 15 seconds."""
        otel = telemetry.get()
        if otel is None or time.monotonic() - self._depth_read < 15:
            return
        self._depth_read = time.monotonic()
        otel.queue_depth.clear()
        otel.queue_depth.update(await self.store.queue_depth())
        otel.busy[self.worker_id] = len(self._tasks)

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
        otel = telemetry.get()
        if otel is not None:
            otel.busy[self.worker_id] = len(self._tasks)
        try:
            with trace.bound(), self._span(otel, job) as spans:
                if spans is not None:
                    trace.listeners.append(spans.record)
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
        status = await self.store.finish_job(
            job["id"], self.worker_id, ok=outcome["ok"], error=outcome["error"],
            max_attempts=job["attempts"] if outcome.get("permanent") else self.max_attempts)
        if otel is not None:
            otel.jobs.add(1, {"kind": job["kind"], "result": str(status or "")})
            otel.busy[self.worker_id] = max(0, len(self._tasks) - 1)

    @contextlib.contextmanager
    def _span(self, otel, job: dict):
        """The job's root span when telemetry is on; nothing otherwise."""
        if otel is None:
            yield None
            return
        with otel.run_span(job["thread_id"], job_kind=job["kind"],
                           attempt=job["attempts"], worker=self.worker_id) as spans:
            yield spans

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
        from .version import orchestration_of, run_version

        thread_id = job["thread_id"]
        run = await self.store.get_run(thread_id) or {}
        stop = await self.store.stop_request(thread_id)
        if stop:
            control.request_stop(stop["by"])
        mode = run.get("mode") or job["payload"].get("mode") or ""
        settings = self.services.settings
        # A run keeps the graph it started with: its checkpoint belongs to it.
        orchestration = (orchestration_of(run.get("version", ""))
                         or job["payload"].get("orchestration", ""))
        if job["kind"] in (START, RECONCILE) and not orchestration:
            orchestration = await self._within_daily_budget(thread_id, trace)
            if orchestration is None:
                return

        def on_event(kind: str, data: dict) -> None:
            trace.event(kind, data)
            if self.on_event is not None:
                self.on_event(thread_id, kind, data)

        graph, ctx = run_session(self.services, control=control, on_event=on_event,
                                 backend=self.backend, mode=mode, orchestration=orchestration,
                                 shots_dir=str(self.trace_dir / "evidence" / thread_id))
        version = run.get("version") or run_version(settings, orchestration)
        config = run_config(thread_id)
        snapshot = await graph.aget_state(config)
        started = time.monotonic()

        if job["kind"] in (START, RECONCILE):
            if stop and not snapshot.values:
                await self.store.upsert_run(thread_id, status="stopped",
                                            error=f"stopped by {stop['by']} before it started")
                return
            await self.store.upsert_run(thread_id, status="running", version=version)
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
            await self._note_wait(thread_id, job)
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
        report["version"] = version
        reason = report.get("halt_reason", "") or ""
        status = "stopped" if report["halted"] and reason.startswith("stopped") else "done"
        await self.store.upsert_run(thread_id, status=status, report=report,
                                    run_id=report.get("run_id") or ctx.run_id)
        trace.write({"service": "run", "kind": "finished", "status": status,
                     "halted": report["halted"], "halt_reason": reason,
                     "version": version, "metrics": report.get("metrics") or {}})
        if reason.startswith(MODEL_DOWN):
            await self._fall_back(run, thread_id, trace)

    async def _within_daily_budget(self, thread_id: str, trace) -> str | None:
        """The orchestration a new run may use under the daily token cap.

        Empty: the deployment's. ``"deterministic"``: the cap is spent and the
        deployment degrades rather than stops. None: the cap is spent and the
        run is refused (recorded on the run, not retried).
        """
        settings = self.services.settings
        cap = int(getattr(settings, "max_tokens_per_day", 0) or 0)
        if not cap or not self.services.agentic:
            return ""
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                      microsecond=0)
        used = await self.store.tokens_since(midnight)
        if used < cap:
            return ""
        trace.write({"service": "run", "kind": "token_cap", "used": used, "cap": cap})
        if getattr(settings, "degrade_on_model_failure", False):
            log.warning("daily_token_cap_degraded", thread_id=thread_id, used=used, cap=cap)
            return "deterministic"
        log.warning("daily_token_cap_refused", thread_id=thread_id, used=used, cap=cap)
        await self.store.upsert_run(
            thread_id, status="failed",
            error=f"the daily model token budget is used ({used} of {cap}); the run "
                  "was not started. It resets at 00:00 UTC (ALM_MAX_TOKENS_PER_DAY).")
        return None

    async def _fall_back(self, run: dict, thread_id: str, trace) -> None:
        """The model was down: re-do the run without it, if the deployment says so.

        A new run (``<thread>-fallback``) with the fixed node sequence, the same
        scope, mode and requester. Writes the first run made are replays in the
        ledger, and a writing run still stops at the approval card.
        """
        settings = self.services.settings
        if (not getattr(settings, "degrade_on_model_failure", False)
                or run.get("trigger") == FALLBACK):
            return
        fallback = f"{thread_id}-fallback"
        job = await submit_run(
            self.store, thread_id=fallback, work_item_ids=run.get("scope") or None,
            mode=run.get("mode") or "", requested_by=run.get("requested_by") or "",
            trigger=FALLBACK, operator_request=run.get("operator_request") or "",
            environment=run.get("environment") or "", orchestration="deterministic")
        trace.write({"service": "run", "kind": "fallback_queued", "thread_id": fallback,
                     "queued": job is not None})
        log.warning("model_down_fallback", thread_id=thread_id, fallback=fallback)


    async def _note_wait(self, thread_id: str, job: dict) -> None:
        """Approval wait time, from the card to the decision now resuming it."""
        otel = telemetry.get()
        if otel is None or job["payload"].get("decision") is None:
            return
        request, _ = await self.store.get_approval(thread_id)
        if request is not None:
            waited = (datetime.now(timezone.utc) - request.created_at).total_seconds()
            otel.approval_wait.record(max(0.0, waited),
                                      {"environment": request.environment or ""})

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
    resolver = build_resolver(settings, interactive=False)
    preflight(settings, resolver)
    notifier = make_notifier(settings, resolver)
    telemetry.setup(settings, service="alm-worker")
    try:
        async with build_services(settings, notifier=notifier, resolver=resolver) as services:
            await Worker(services).run_forever(stopping)
    finally:
        telemetry.shutdown()


def preflight(settings, resolver) -> None:
    """Refuse to start a worker that could only fail every run.

    A worker drives EWM and JTS as the service account; without its CID and
    password every run would fail at sign-in, one retry at a time. Checked
    once, at start-up, naming what is missing - never the value.
    """
    from alm_core.errors import CredentialError

    if getattr(settings, "orchestration", "") == "deterministic" and not settings.ewm_server:
        return  # a smoke or test configuration with no estate to reach
    settings.require("service_account", "ewm_server", "jts_server")
    try:
        resolver.get(settings.password_secret_name)
    except CredentialError as err:
        raise ConfigError(
            f"no password for the service account {settings.service_account}: set the "
            f"secret {settings.password_secret_name!r} (cloud secret store, mounted "
            "file, or EWM_PASSWORD)") from err


async def queue_synthetic(work_item_id: str, settings=None) -> str:
    """Queue a dry run of one work item, for a scheduled end-to-end check.

    A dry run writes nothing, so it is safe against the real estate. It still
    signs in to EWM and JTS, asks the model and goes through the queue, so a
    missing ``done`` run in the metrics means something on that path is broken.
    """
    from alm_core.config import get_settings
    from alm_core.store import get_store

    settings = settings or get_settings()
    stamp = time.strftime("%Y%m%dT%H%M", time.gmtime())
    thread_id = f"synthetic-{work_item_id}-{stamp}"
    store = await get_store(settings)
    try:
        await submit_run(store, thread_id=thread_id, work_item_ids=[work_item_id],
                         mode="dry", requested_by="synthetic", trigger="synthetic",
                         environment=getattr(settings, "environment", ""))
    finally:
        await store.close()
    return thread_id


def main(argv: list[str] | None = None) -> int:
    import signal
    import sys

    from alm_core.logging import configure

    configure()
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["synthetic"]:
        if len(args) != 2 or not args[1].isdigit():
            print("usage: python -m alm_agents.worker synthetic <work item number>")
            return 2
        print(asyncio.run(queue_synthetic(args[1])))
        return 0
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
