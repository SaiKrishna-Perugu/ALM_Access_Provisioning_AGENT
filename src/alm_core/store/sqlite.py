"""Durable local state in one SQLite file: ledger, audit trail, approvals.

The local counterpart of :mod:`alm_core.store.postgres`, with the same
semantics, so a laptop run is as safe to repeat as a cloud run:

* **The idempotency ledger** - claimed, completed, in flight or failed, with the
  same 15-minute lease. A re-run of a work item reports earlier writes as
  replays instead of repeating them.
* **The audit trail is append-only**, enforced by triggers rather than by
  convention: an UPDATE or DELETE on ``alm_audit`` raises.
* **Approvals** - a decision belongs to the plan it was made on; a new plan on
  the same thread starts undecided.

One connection, serialised by a lock: SQLite allows one writer at a time anyway,
and the agents' writes are few. Claims run inside ``BEGIN IMMEDIATE`` so the
check-then-insert cannot interleave with another process on the same file.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from ..errors import ConfigError, IdempotencyViolation
from ..logging import get_logger
from ..models import ApprovalDecision, ApprovalRequest, AuditEvent, Operation, ProvisionResult
from .postgres import CLAIM_LEASE

log = get_logger("alm.store.sqlite")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alm_idempotency (
    key             TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    work_item_id    TEXT NOT NULL,
    userid          TEXT NOT NULL,
    operation       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('in_flight', 'completed', 'failed')),
    result          TEXT,
    claimed_at      TEXT NOT NULL,
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS alm_idempotency_run  ON alm_idempotency (run_id);
CREATE INDEX IF NOT EXISTS alm_idempotency_user ON alm_idempotency (userid);

CREATE TABLE IF NOT EXISTS alm_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    thread_id       TEXT NOT NULL DEFAULT '',
    at              TEXT NOT NULL,
    environment     TEXT NOT NULL DEFAULT '',
    step            TEXT NOT NULL,
    userid          TEXT NOT NULL DEFAULT '',
    work_item_id    TEXT NOT NULL DEFAULT '',
    operation       TEXT,
    outcome         TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    approver        TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    error_type      TEXT NOT NULL DEFAULT '',
    detail          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS alm_audit_run  ON alm_audit (run_id, at);
CREATE INDEX IF NOT EXISTS alm_audit_user ON alm_audit (userid, at);

-- An audit trail that can be edited is not an audit trail.
CREATE TRIGGER IF NOT EXISTS alm_audit_no_update BEFORE UPDATE ON alm_audit
BEGIN SELECT RAISE(ABORT, 'alm_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS alm_audit_no_delete BEFORE DELETE ON alm_audit
BEGIN SELECT RAISE(ABORT, 'alm_audit is append-only'); END;

CREATE TABLE IF NOT EXISTS alm_approval (
    thread_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    environment     TEXT NOT NULL DEFAULT '',
    plan_hash       TEXT NOT NULL,
    request         TEXT NOT NULL,
    decision        TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS alm_approval_run ON alm_approval (run_id);
"""


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: datetime) -> str:
    """UTC ISO-8601, which sorts correctly as text."""
    return value.astimezone(timezone.utc).isoformat()


class SqliteStore:
    """The ledger, audit trail and approvals in a single local file."""

    durable = True

    def __init__(self, path: str):
        if not path:
            raise ConfigError("SqliteStore needs a file path (ALM_LEDGER_PATH)")
        self.path = path
        self._db = None
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._db is not None:
            return
        try:
            import aiosqlite
        except ImportError as err:  # pragma: no cover
            raise ConfigError("aiosqlite is required for the local ledger "
                              "(pip install -r requirements-cloud.txt)") from err
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        # isolation_level=None: autocommit, with explicit transactions where the
        # read-then-write must be atomic.
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=10000")
        log.info("store_opened", path=self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def migrate(self) -> None:
        await self._conn().executescript(SCHEMA_SQL)

    def _conn(self):
        if self._db is None:
            raise ConfigError("SqliteStore.start() has not been awaited")
        return self._db

    async def _fetchone(self, sql: str, params: tuple = ()):
        async with self._conn().execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()):
        async with self._conn().execute(sql, params) as cursor:
            return await cursor.fetchall()

    # --------------------------------------------------------- idempotency

    async def claim(self, key: str, *, run_id: str, work_item_id: str, userid: str,
                    operation: Operation) -> tuple[bool, ProvisionResult | None]:
        """Same contract as ``PostgresStore.claim``."""
        now = _now()
        async with self._lock:
            db = self._conn()
            try:
                await db.execute("BEGIN IMMEDIATE")
            except Exception as err:  # sqlite3.OperationalError: database is locked
                raise IdempotencyViolation(
                    "the local ledger is locked by another process; try again when it "
                    "finishes", context={"key": key, "error": str(err)}) from err
            try:
                row = await self._fetchone(
                    "SELECT status, result, claimed_at, run_id FROM alm_idempotency "
                    "WHERE key = ?", (key,))
                if row is None:
                    await db.execute(
                        "INSERT INTO alm_idempotency (key, run_id, work_item_id, userid, "
                        "operation, status, claimed_at) VALUES (?, ?, ?, ?, ?, 'in_flight', ?)",
                        (key, run_id, work_item_id, userid, operation.value, _ts(now)))
                    await db.execute("COMMIT")
                    return True, None

                status, result, claimed_at, owner = row
                if status == "completed":
                    await db.execute("COMMIT")
                    log.info("idempotent_replay", key=key, userid=userid,
                             operation=operation.value, original_run=owner)
                    return False, (ProvisionResult.model_validate_json(result)
                                   if result else None)

                if status == "in_flight" and \
                        now - datetime.fromisoformat(claimed_at) <= CLAIM_LEASE:
                    await db.execute("COMMIT")
                    raise IdempotencyViolation(
                        "another worker is performing this write",
                        context={"key": key, "owner_run": owner, "userid": userid,
                                 "operation": operation.value})

                # A stale in-flight claim (its owner died) or a failed attempt:
                # take it over and try again.
                if status == "in_flight":
                    log.warning("idempotency_claim_taken_over", key=key,
                                previous_run=owner, stale_since=claimed_at)
                await db.execute(
                    "UPDATE alm_idempotency SET status = 'in_flight', run_id = ?, "
                    "claimed_at = ?, completed_at = NULL WHERE key = ?",
                    (run_id, _ts(now), key))
                await db.execute("COMMIT")
                return True, None
            except IdempotencyViolation:
                raise
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def complete(self, key: str, result: ProvisionResult) -> None:
        # An outcome nobody can confirm is closed, not retried: see OutcomeUnknown.
        status = ("completed" if result.succeeded
                  or result.detail.get("outcome_unknown") else "failed")
        async with self._lock:
            await self._conn().execute(
                "UPDATE alm_idempotency SET status = ?, result = ?, completed_at = ? "
                "WHERE key = ?",
                (status, _json(result.model_dump(mode="json")), _ts(_now()), key))

    async def release(self, key: str) -> None:
        async with self._lock:
            await self._conn().execute(
                "UPDATE alm_idempotency SET status = 'failed', completed_at = ? "
                "WHERE key = ? AND status = 'in_flight'", (_ts(_now()), key))

    # --------------------------------------------------------------- audit

    async def record(self, event: AuditEvent) -> None:
        async with self._lock:
            await self._conn().execute(
                "INSERT INTO alm_audit (run_id, thread_id, at, environment, step, userid, "
                "work_item_id, operation, outcome, idempotency_key, approver, message, "
                "error_type, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.run_id, event.thread_id, _ts(event.at), event.environment,
                 event.step, event.userid, event.work_item_id,
                 event.operation.value if event.operation else None,
                 event.outcome.value, event.idempotency_key, event.approver,
                 event.message, event.error_type, _json(event.detail)))

    async def record_many(self, events: list[AuditEvent]) -> None:
        for event in events:
            await self.record(event)

    async def run_events(self, run_id: str) -> list[dict]:
        columns = ["run_id", "thread_id", "at", "environment", "step", "userid",
                   "work_item_id", "operation", "outcome", "idempotency_key", "approver",
                   "message", "error_type", "detail"]
        rows = await self._fetchall(
            f"SELECT {', '.join(columns)} FROM alm_audit WHERE run_id = ? ORDER BY at, id",
            (run_id,))
        events = []
        for row in rows:
            event = dict(zip(columns, row, strict=True))
            event["detail"] = json.loads(event["detail"] or "{}")
            events.append(event)
        return events

    async def recent_runs(self, limit: int = 50) -> list[dict]:
        rows = await self._fetchall(
            "SELECT run_id, min(at), max(at), count(*), "
            "sum(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) "
            "FROM alm_audit GROUP BY run_id ORDER BY min(at) DESC LIMIT ?", (limit,))
        return [{"run_id": r[0], "started": r[1], "last_event": r[2],
                 "events": r[3], "failures": r[4]} for r in rows]

    # ------------------------------------------------------------ approval

    async def save_approval_request(self, request: ApprovalRequest) -> None:
        async with self._lock:
            # Right-hand sides of DO UPDATE read the pre-update row, as in Postgres.
            await self._conn().execute(
                """
                INSERT INTO alm_approval (thread_id, run_id, environment, plan_hash,
                                          request, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (thread_id) DO UPDATE
                   SET request = excluded.request, plan_hash = excluded.plan_hash,
                       expires_at = excluded.expires_at,
                       decision = CASE WHEN alm_approval.plan_hash = excluded.plan_hash
                                       THEN alm_approval.decision END,
                       decided_at = CASE WHEN alm_approval.plan_hash = excluded.plan_hash
                                         THEN alm_approval.decided_at END
                """,
                (request.thread_id, request.run_id, request.environment, request.plan_hash,
                 _json(request.model_dump(mode="json")), _ts(request.created_at),
                 _ts(request.expires_at)))

    async def save_approval_decision(self, decision: ApprovalDecision) -> None:
        async with self._lock:
            await self._conn().execute(
                "UPDATE alm_approval SET decision = ?, decided_at = ? WHERE thread_id = ?",
                (_json(decision.model_dump(mode="json")), _ts(decision.decided_at),
                 decision.thread_id))

    async def get_approval(self, thread_id: str
                           ) -> tuple[ApprovalRequest | None, ApprovalDecision | None]:
        row = await self._fetchone(
            "SELECT request, decision FROM alm_approval WHERE thread_id = ?", (thread_id,))
        if not row:
            return None, None
        request = ApprovalRequest.model_validate_json(row[0]) if row[0] else None
        decision = ApprovalDecision.model_validate_json(row[1]) if row[1] else None
        return request, decision
