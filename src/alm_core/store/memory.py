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

log = get_logger("alm.store.memory")

CLAIM_LEASE = timedelta(minutes=15)


class MemoryStore:
    """Non-durable store. Read the module docstring before using it for writes."""

    durable = False

    def __init__(self):
        self._claims: dict[str, dict] = {}
        self._audit: list[AuditEvent] = []
        self._approvals: dict[str, tuple[ApprovalRequest, ApprovalDecision | None]] = {}
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
            entry["status"] = "completed" if result.succeeded else "failed"
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
        _existing, decision = self._approvals.get(request.thread_id, (None, None))
        self._approvals[request.thread_id] = (request, decision)

    async def save_approval_decision(self, decision: ApprovalDecision) -> None:
        request, _ = self._approvals.get(decision.thread_id, (None, None))
        if request is None:
            return
        self._approvals[decision.thread_id] = (request, decision)

    async def get_approval(self, thread_id: str
                           ) -> tuple[ApprovalRequest | None, ApprovalDecision | None]:
        return self._approvals.get(thread_id, (None, None))
