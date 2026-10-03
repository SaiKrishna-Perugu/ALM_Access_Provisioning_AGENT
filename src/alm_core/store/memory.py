"""In-process store for shadow mode and local development.

Same interface as :class:`~alm_core.store.postgres.PostgresStore`, so the graph
runs unchanged on a laptop with no database. It is explicitly *not* durable:
nothing survives a restart, and two processes share nothing. That is safe only
because shadow mode performs no writes - the moment ``shadow_mode`` is off,
:func:`alm_core.store.get_store` refuses to hand this one out.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ..errors import IdempotencyViolation
from ..logging import get_logger
from ..models import ApprovalDecision, ApprovalRequest, AuditEvent, Operation, ProvisionResult
from .runs import next_job_status, run_values

log = get_logger("alm.store.memory")

CLAIM_LEASE = timedelta(minutes=15)


class MemoryStore:
    """Non-durable store. Read the module docstring before using it for writes."""

    durable = False

    def __init__(self):
        self._claims: dict[str, dict] = {}
        self._audit: list[AuditEvent] = []
        self._approvals: dict[str, tuple[ApprovalRequest, ApprovalDecision | None]] = {}
        self._runs: dict[str, dict] = {}
        self._jobs: list[dict] = []
        self._stops: dict[str, dict] = {}
        self._seen: dict[str, datetime] = {}
        self._leases: dict[str, tuple[str, datetime]] = {}
        self._traces: list[tuple[str, dict]] = []
        self._votes: dict[tuple[str, str], list[dict]] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def migrate(self) -> None:
        return None

    # --------------------------------------------------------- idempotency

    async def claim(self, key: str, *, run_id: str, work_item_id: str, userid: str,
                    operation: Operation) -> tuple[bool, ProvisionResult | None]:
        now = datetime.now(timezone.utc)
        async with self._lock:
            entry = self._claims.get(key)
            if entry is None:
                self._claims[key] = {"status": "in_flight", "run_id": run_id,
                                     "claimed_at": now, "result": None,
                                     "work_item_id": work_item_id, "userid": userid,
                                     "operation": operation.value}
                return True, None
            if entry["status"] == "completed":
                return False, entry["result"]
            if entry["status"] == "failed":
                entry.update(status="in_flight", run_id=run_id, claimed_at=now)
                return True, None
            if now - entry["claimed_at"] > CLAIM_LEASE:
                entry.update(run_id=run_id, claimed_at=now)
                return True, None
            raise IdempotencyViolation(
                "another task is performing this write",
                context={"key": key, "owner_run": entry["run_id"], "userid": userid})

    async def complete(self, key: str, result: ProvisionResult) -> None:
        async with self._lock:
            entry = self._claims.setdefault(key, {})
            # An outcome nobody can confirm is closed, not retried: see OutcomeUnknown.
            entry["status"] = ("completed" if result.succeeded
                               or result.detail.get("outcome_unknown") else "failed")
            entry["result"] = result
            entry["completed_at"] = datetime.now(timezone.utc)

    async def release(self, key: str) -> None:
        async with self._lock:
            entry = self._claims.get(key)
            if entry and entry.get("status") == "in_flight":
                entry["status"] = "failed"

    # --------------------------------------------------------------- audit

    async def record(self, event: AuditEvent) -> None:
        self._audit.append(event)
        log.info("audit", step=event.step, userid=event.userid,
                 outcome=event.outcome.value, work_item=event.work_item_id,
                 message=event.message)

    async def record_many(self, events: list[AuditEvent]) -> None:
        for event in events:
            await self.record(event)

    async def run_events(self, run_id: str) -> list[dict]:
        return [e.model_dump(mode="json") for e in self._audit if e.run_id == run_id]

    async def recent_runs(self, limit: int = 50) -> list[dict]:
        runs: dict[str, dict] = {}
        for event in self._audit:
            row = runs.setdefault(event.run_id, {"run_id": event.run_id, "events": 0,
                                                 "failures": 0, "started": event.at,
                                                 "last_event": event.at})
            row["events"] += 1
            row["failures"] += int(event.outcome.value == "failed")
            row["started"] = min(row["started"], event.at)
            row["last_event"] = max(row["last_event"], event.at)
        return sorted(runs.values(), key=lambda r: r["started"], reverse=True)[:limit]

    # ------------------------------------------------------------ approval

    async def save_approval_request(self, request: ApprovalRequest) -> None:
        # A decision belongs to the plan it was made on. A new plan on the same
        # thread (the agentic graph can ask twice) starts undecided.
        existing, decision = self._approvals.get(request.thread_id, (None, None))
        if existing is None or existing.plan_hash != request.plan_hash:
            decision = None
        self._approvals[request.thread_id] = (request, decision)

    async def save_approval_decision(self, decision: ApprovalDecision) -> None:
        request, _ = self._approvals.get(decision.thread_id, (None, None))
        if request is None:
            return
        self._approvals[decision.thread_id] = (request, decision)

    async def get_approval(self, thread_id: str
                           ) -> tuple[ApprovalRequest | None, ApprovalDecision | None]:
        return self._approvals.get(thread_id, (None, None))

    # ---------------------------------------------------------------- runs

    async def upsert_run(self, thread_id: str, **fields) -> None:
        values = run_values(fields, json_as_text=False)
        now = _iso(_now())
        run = self._runs.setdefault(thread_id, {
            "thread_id": thread_id, "run_id": "", "status": "queued", "mode": "",
            "scope": [], "requested_by": "", "trigger": "", "operator_request": "",
            "environment": "", "version": "", "error": "", "report": None,
            "created_at": now, "updated_at": now})
        run.update(values, updated_at=now)

    async def get_run(self, thread_id: str) -> dict | None:
        run = self._runs.get(thread_id)
        return dict(run) if run else None

    async def list_runs(self, limit: int = 50) -> list[dict]:
        runs = sorted(self._runs.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in runs[:limit]]

    # --------------------------------------------------------------- queue

    async def enqueue_job(self, kind: str, thread_id: str, payload: dict | None = None,
                          *, dedupe: bool = False, delay_seconds: float = 0) -> int | None:
        async with self._lock:
            if dedupe and any(j["thread_id"] == thread_id and j["kind"] == kind
                              and j["status"] in ("queued", "running") for j in self._jobs):
                return None
            now = _now()
            job = {"id": len(self._jobs) + 1, "kind": kind, "thread_id": thread_id,
                   "payload": dict(payload or {}), "status": "queued", "attempts": 0,
                   "available_at": now + timedelta(seconds=delay_seconds),
                   "locked_by": "", "locked_until": None, "error": "",
                   "created_at": now, "finished_at": None}
            self._jobs.append(job)
            return job["id"]

    async def claim_job(self, worker: str, lease_seconds: float,
                        *, max_attempts: int = 5,
                        kinds: tuple[str, ...] | None = None) -> dict | None:
        now = _now()
        async with self._lock:
            held = {j["thread_id"] for j in self._jobs
                    if j["status"] == "running" and j["locked_until"] >= now}
            for job in self._jobs:
                ready = ((job["status"] == "queued" and job["available_at"] <= now)
                         or (job["status"] == "running" and job["locked_until"] < now))
                if not ready or job["thread_id"] in held or (kinds and job["kind"] not in kinds):
                    continue
                if job["attempts"] >= max_attempts:
                    job.update(status="dead", finished_at=now,
                               error=f"gave up after {job['attempts']} attempt(s)")
                    continue
                job.update(status="running", locked_by=worker, attempts=job["attempts"] + 1,
                           locked_until=now + timedelta(seconds=lease_seconds))
                return _job_view(job)
            return None

    async def extend_job(self, job_id: int, worker: str, lease_seconds: float) -> bool:
        async with self._lock:
            job = self._job(job_id)
            if job and job["locked_by"] == worker and job["status"] == "running":
                job["locked_until"] = _now() + timedelta(seconds=lease_seconds)
                return True
            return False

    async def finish_job(self, job_id: int, worker: str, *, ok: bool, error: str = "",
                         max_attempts: int = 5, retry_seconds: float = 60) -> str:
        now = _now()
        async with self._lock:
            job = self._job(job_id)
            if not job or job["locked_by"] != worker or job["status"] != "running":
                return "lost"
            status = next_job_status(ok, job["attempts"], max_attempts)
            job.update(status=status, error=error[:2000], locked_until=None,
                       available_at=now + timedelta(seconds=retry_seconds * job["attempts"]),
                       finished_at=None if status == "queued" else now)
            return status

    async def queue_depth(self) -> dict:
        depth: dict[str, int] = {}
        for job in self._jobs:
            depth[job["status"]] = depth.get(job["status"], 0) + 1
        return depth

    async def list_jobs(self, *, status: str = "", limit: int = 100) -> list[dict]:
        jobs = [j for j in reversed(self._jobs) if not status or j["status"] == status]
        return [_job_view(j) for j in jobs[:limit]]

    def _job(self, job_id: int) -> dict | None:
        return next((j for j in self._jobs if j["id"] == job_id), None)

    # ------------------------------------------------------------- control

    async def request_stop(self, thread_id: str, by: str) -> None:
        self._stops[thread_id] = {"by": by, "at": _iso(_now())}

    async def stop_request(self, thread_id: str) -> dict | None:
        stop = self._stops.get(thread_id)
        return dict(stop) if stop else None

    async def clear_stop(self, thread_id: str) -> None:
        self._stops.pop(thread_id, None)

    # ------------------------------------------------- replay and leadership

    async def remember_delivery(self, delivery_id: str, *,
                                ttl_seconds: float = 86400) -> bool:
        now = _now()
        for seen, at in list(self._seen.items()):
            if now - at > timedelta(seconds=ttl_seconds):
                del self._seen[seen]
        if delivery_id in self._seen:
            return False
        self._seen[delivery_id] = now
        return True

    async def try_lease(self, name: str, holder: str, seconds: float) -> bool:
        now = _now()
        current = self._leases.get(name)
        if current is None or current[0] == holder or current[1] < now:
            self._leases[name] = (holder, now + timedelta(seconds=seconds))
            return True
        return False

    # --------------------------------------------------------------- trace

    async def record_trace(self, thread_id: str, records: list[dict]) -> None:
        self._traces.extend((thread_id, dict(r)) for r in records)

    async def trace_since(self, thread_id: str, after: int = 0,
                          limit: int = 1000) -> list[dict]:
        out = [{**record, "cursor": cursor}
               for cursor, (thread, record) in enumerate(self._traces, start=1)
               if thread == thread_id and cursor > after]
        return out[:limit]


    # --------------------------------------------------------------- votes

    async def add_vote(self, thread_id: str, plan_hash: str, approver: str, *,
                       approved: bool, userids: list[str], comment: str = "") -> bool:
        votes = self._votes.setdefault((thread_id, plan_hash), [])
        if any(v["approver"] == approver.lower() for v in votes):
            return False
        votes.append({"approver": approver.lower(), "approved": approved,
                      "userids": list(userids), "comment": comment[:500],
                      "at": _iso(_now())})
        return True

    async def votes(self, thread_id: str, plan_hash: str) -> list[dict]:
        return [dict(v) for v in self._votes.get((thread_id, plan_hash), [])]

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _job_view(job: dict) -> dict:
    view = dict(job, payload=dict(job["payload"]))
    for column in ("available_at", "locked_until", "created_at", "finished_at"):
        view[column] = _iso(view[column]) if view[column] else ""
    return view
