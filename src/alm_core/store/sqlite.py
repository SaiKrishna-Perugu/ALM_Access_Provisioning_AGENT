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
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import ConfigError, IdempotencyViolation
from ..logging import get_logger
from ..models import ApprovalDecision, ApprovalRequest, AuditEvent, Operation, ProvisionResult
from .postgres import CLAIM_LEASE
from .runs import (
    JOB_SELECT,
    RUN_SELECT,
    job_row,
    next_job_status,
    run_row,
    run_values,
)

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

# Schema changes are appended here, never edited: (version, SQL to reach it).
# Version 1 is SCHEMA_SQL itself, which every ledger file already has. A new
# column, say, becomes (2, "ALTER TABLE alm_audit ADD COLUMN ...;").
MIGRATIONS: list[tuple[int, str]] = [
    (1, ""),
    # Version 2: what lets several processes share the work - the run registry,
    # the job queue, stop requests, webhook replay protection, leases, traces.
    (2, """
CREATE TABLE IF NOT EXISTS alm_run (
    thread_id        TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'queued',
    mode             TEXT NOT NULL DEFAULT '',
    scope            TEXT NOT NULL DEFAULT '[]',
    requested_by     TEXT NOT NULL DEFAULT '',
    trigger          TEXT NOT NULL DEFAULT '',
    operator_request TEXT NOT NULL DEFAULT '',
    environment      TEXT NOT NULL DEFAULT '',
    version          TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    report           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alm_run_updated ON alm_run (updated_at);

CREATE TABLE IF NOT EXISTS alm_run_job (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    thread_id    TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'dead')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_by    TEXT NOT NULL DEFAULT '',
    locked_until TEXT,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS alm_run_job_ready ON alm_run_job (status, available_at);
CREATE INDEX IF NOT EXISTS alm_run_job_thread ON alm_run_job (thread_id, status);

CREATE TABLE IF NOT EXISTS alm_run_control (
    thread_id    TEXT PRIMARY KEY,
    stop_by      TEXT NOT NULL,
    stop_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alm_webhook_seen (
    delivery_id  TEXT PRIMARY KEY,
    seen_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alm_lease (
    name         TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    until        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alm_trace_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    record       TEXT NOT NULL,
    at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alm_trace_thread ON alm_trace_event (thread_id, seq);
"""),
    # Version 3: one row per approver per plan, for the two-person rule.
    (3, """
CREATE TABLE IF NOT EXISTS alm_approval_vote (
    thread_id    TEXT NOT NULL,
    plan_hash    TEXT NOT NULL,
    approver     TEXT NOT NULL,
    approved     INTEGER NOT NULL,
    userids      TEXT NOT NULL DEFAULT '[]',
    comment      TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL,
    PRIMARY KEY (thread_id, plan_hash, approver)
);
"""),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


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
        """Create the tables, then bring the file's schema up to this code's version.

        The version lives in the file, so a ``git pull`` that changes the schema
        upgrades an existing ledger step by step - and a ledger written by newer
        code is refused rather than misread.
        """
        db = self._conn()
        await db.executescript(SCHEMA_SQL)
        await db.execute("CREATE TABLE IF NOT EXISTS alm_schema_version "
                         "(version INTEGER NOT NULL)")
        row = await self._fetchone("SELECT MAX(version) FROM alm_schema_version")
        current = row[0] if row and row[0] is not None else 0
        if current > SCHEMA_VERSION:
            raise ConfigError(
                f"{self.path} was written by a newer version of this tool (schema "
                f"{current}; this code knows {SCHEMA_VERSION}). Update the code "
                "(git pull) before using this ledger.")
        for version, statements in MIGRATIONS:
            if version > current:
                if statements:
                    await db.executescript(statements)
                await db.execute("INSERT INTO alm_schema_version (version) VALUES (?)",
                                 (version,))
                log.info("ledger_schema_migrated", path=self.path, version=version)

    async def schema_version(self) -> int:
        row = await self._fetchone("SELECT MAX(version) FROM alm_schema_version")
        return row[0] if row and row[0] is not None else 0

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
            f"SELECT {', '.join(columns)} FROM alm_audit WHERE run_id = ? ORDER BY at, id",  # noqa: S608 - column list is a fixed list of static schema columns
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

    # ---------------------------------------------------------------- runs

    async def upsert_run(self, thread_id: str, **fields) -> None:
        """Create or update a run's registry row. Unknown fields are refused."""
        values = run_values(fields)
        now = _ts(_now())
        async with self._lock:
            db = self._conn()
            await db.execute(
                "INSERT INTO alm_run (thread_id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (thread_id) DO NOTHING", (thread_id, now, now))
            if values:
                assignments = ", ".join(f"{column} = ?" for column in values)
                await db.execute(
                    f"UPDATE alm_run SET {assignments}, updated_at = ? WHERE thread_id = ?",  # noqa: S608 - columns come from RUN_COLUMNS, never input
                    (*values.values(), now, thread_id))

    async def get_run(self, thread_id: str) -> dict | None:
        row = await self._fetchone(
            f"SELECT {', '.join(RUN_SELECT)} FROM alm_run WHERE thread_id = ?",  # noqa: S608 - fixed column list
            (thread_id,))
        return run_row(row) if row else None

    async def list_runs(self, limit: int = 50) -> list[dict]:
        rows = await self._fetchall(
            f"SELECT {', '.join(RUN_SELECT)} FROM alm_run "  # noqa: S608 - fixed column list
            "ORDER BY created_at DESC LIMIT ?", (limit,))
        return [run_row(r) for r in rows]

    # --------------------------------------------------------------- queue

    async def enqueue_job(self, kind: str, thread_id: str, payload: dict | None = None,
                          *, dedupe: bool = False, delay_seconds: float = 0) -> int | None:
        """Queue a job. With ``dedupe``, an unfinished job of the same kind for the
        same thread makes this a no-op (a redelivered webhook, say): returns None."""
        now = _now()
        async with self._lock:
            db = self._conn()
            await db.execute("BEGIN IMMEDIATE")
            try:
                if dedupe and await self._fetchone(
                        "SELECT 1 FROM alm_run_job WHERE thread_id = ? AND kind = ? "
                        "AND status IN ('queued', 'running')", (thread_id, kind)):
                    await db.execute("COMMIT")
                    return None
                cursor = await db.execute(
                    "INSERT INTO alm_run_job (kind, thread_id, payload, available_at, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (kind, thread_id, _json(payload or {}),
                     _ts(now + timedelta(seconds=delay_seconds)), _ts(now)))
                await db.execute("COMMIT")
                return cursor.lastrowid
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def claim_job(self, worker: str, lease_seconds: float,
                        *, max_attempts: int = 5,
                        kinds: tuple[str, ...] | None = None) -> dict | None:
        """Take the oldest ready job, never one whose thread another worker holds.

        A running job whose lease has expired is ready again: its worker died.
        A job that has already used ``max_attempts`` goes to ``dead`` instead.
        """
        now = _now()
        async with self._lock:
            db = self._conn()
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await self._fetchall(
                    f"SELECT {', '.join(JOB_SELECT)} FROM alm_run_job "  # noqa: S608 - fixed column list
                    "WHERE (status = 'queued' AND available_at <= ?) "
                    "   OR (status = 'running' AND locked_until < ?) ORDER BY id",
                    (_ts(now), _ts(now)))
                held = {r[0] for r in await self._fetchall(
                    "SELECT thread_id FROM alm_run_job WHERE status = 'running' "
                    "AND locked_until >= ?", (_ts(now),))}
                chosen = None
                for row in rows:
                    job = job_row(row)
                    if job["thread_id"] in held or (kinds and job["kind"] not in kinds):
                        continue
                    if job["attempts"] >= max_attempts:
                        await db.execute(
                            "UPDATE alm_run_job SET status = 'dead', finished_at = ?, "
                            "error = ? WHERE id = ?",
                            (_ts(now), f"gave up after {job['attempts']} attempt(s)",
                             job["id"]))
                        continue
                    chosen = job
                    break
                if chosen is not None:
                    await db.execute(
                        "UPDATE alm_run_job SET status = 'running', locked_by = ?, "
                        "locked_until = ?, attempts = attempts + 1 WHERE id = ?",
                        (worker, _ts(now + timedelta(seconds=lease_seconds)), chosen["id"]))
                    chosen.update(status="running", locked_by=worker,
                                  attempts=chosen["attempts"] + 1)
                await db.execute("COMMIT")
                return chosen
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def extend_job(self, job_id: int, worker: str, lease_seconds: float) -> bool:
        async with self._lock:
            cursor = await self._conn().execute(
                "UPDATE alm_run_job SET locked_until = ? WHERE id = ? AND locked_by = ? "
                "AND status = 'running'",
                (_ts(_now() + timedelta(seconds=lease_seconds)), job_id, worker))
            return (cursor.rowcount or 0) > 0

    async def finish_job(self, job_id: int, worker: str, *, ok: bool, error: str = "",
                         max_attempts: int = 5, retry_seconds: float = 60) -> str:
        """Close a job. A failure is retried later, with backoff, until
        ``max_attempts``; then it is ``dead`` and waits for a human."""
        now = _now()
        async with self._lock:
            row = await self._fetchone(
                "SELECT attempts FROM alm_run_job WHERE id = ? AND locked_by = ? "
                "AND status = 'running'", (job_id, worker))
            if row is None:
                return "lost"  # the lease expired and another worker took it
            status = next_job_status(ok, row[0], max_attempts)
            await self._conn().execute(
                "UPDATE alm_run_job SET status = ?, error = ?, locked_until = NULL, "
                "available_at = ?, finished_at = ? WHERE id = ?",
                (status, error[:2000], _ts(now + timedelta(seconds=retry_seconds * row[0])),
                 _ts(now) if status != "queued" else None, job_id))
            return status

    async def queue_depth(self) -> dict:
        rows = await self._fetchall("SELECT status, COUNT(*) FROM alm_run_job GROUP BY status")
        return dict(rows)

    async def list_jobs(self, *, status: str = "", limit: int = 100) -> list[dict]:
        if status:
            rows = await self._fetchall(
                f"SELECT {', '.join(JOB_SELECT)} FROM alm_run_job WHERE status = ? "  # noqa: S608 - fixed column list
                "ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = await self._fetchall(
                f"SELECT {', '.join(JOB_SELECT)} FROM alm_run_job "  # noqa: S608 - fixed column list
                "ORDER BY id DESC LIMIT ?", (limit,))
        return [job_row(r) for r in rows]

    # ------------------------------------------------------------- control

    async def request_stop(self, thread_id: str, by: str) -> None:
        async with self._lock:
            await self._conn().execute(
                "INSERT INTO alm_run_control (thread_id, stop_by, stop_at) VALUES (?, ?, ?) "
                "ON CONFLICT (thread_id) DO UPDATE SET stop_by = excluded.stop_by, "
                "stop_at = excluded.stop_at", (thread_id, by, _ts(_now())))

    async def stop_request(self, thread_id: str) -> dict | None:
        row = await self._fetchone(
            "SELECT stop_by, stop_at FROM alm_run_control WHERE thread_id = ?", (thread_id,))
        return {"by": row[0], "at": row[1]} if row else None

    async def clear_stop(self, thread_id: str) -> None:
        async with self._lock:
            await self._conn().execute("DELETE FROM alm_run_control WHERE thread_id = ?",
                                       (thread_id,))

    # ------------------------------------------------- replay and leadership

    async def remember_delivery(self, delivery_id: str, *,
                                ttl_seconds: float = 86400) -> bool:
        """True the first time a webhook delivery id is seen, False on a replay."""
        now = _now()
        async with self._lock:
            db = self._conn()
            await db.execute("DELETE FROM alm_webhook_seen WHERE seen_at < ?",
                             (_ts(now - timedelta(seconds=ttl_seconds)),))
            cursor = await db.execute(
                "INSERT INTO alm_webhook_seen (delivery_id, seen_at) VALUES (?, ?) "
                "ON CONFLICT (delivery_id) DO NOTHING", (delivery_id, _ts(now)))
            return (cursor.rowcount or 0) > 0

    async def try_lease(self, name: str, holder: str, seconds: float) -> bool:
        """Hold (or renew) a named lease. Exactly one holder at a time."""
        now = _now()
        async with self._lock:
            await self._conn().execute(
                "INSERT INTO alm_lease (name, holder, until) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, "
                "until = excluded.until WHERE alm_lease.holder = excluded.holder "
                "OR alm_lease.until < ?",
                (name, holder, _ts(now + timedelta(seconds=seconds)), _ts(now)))
            row = await self._fetchone("SELECT holder FROM alm_lease WHERE name = ?", (name,))
            return bool(row and row[0] == holder)

    # --------------------------------------------------------------- trace

    async def record_trace(self, thread_id: str, records: list[dict]) -> None:
        if not records:
            return
        async with self._lock:
            await self._conn().executemany(
                "INSERT INTO alm_trace_event (thread_id, seq, record, at) VALUES (?, ?, ?, ?)",
                [(thread_id, int(r.get("seq", 0)), _json(r), str(r.get("at") or _ts(_now())))
                 for r in records])

    async def trace_since(self, thread_id: str, after: int = 0,
                          limit: int = 1000) -> list[dict]:
        """Records after cursor ``after``; each carries its ``cursor``."""
        rows = await self._fetchall(
            "SELECT id, record FROM alm_trace_event WHERE thread_id = ? AND id > ? "
            "ORDER BY id LIMIT ?", (thread_id, after, limit))
        return [{**json.loads(record), "cursor": cursor} for cursor, record in rows]

    # --------------------------------------------------------------- votes

    async def add_vote(self, thread_id: str, plan_hash: str, approver: str, *,
                       approved: bool, userids: list[str], comment: str = "") -> bool:
        """Record one approver's vote on one plan. False if they already voted."""
        async with self._lock:
            cursor = await self._conn().execute(
                "INSERT INTO alm_approval_vote (thread_id, plan_hash, approver, approved, "
                "userids, comment, at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (thread_id, plan_hash, approver) DO NOTHING",
                (thread_id, plan_hash, approver.lower(), int(approved), _json(userids),
                 comment[:500], _ts(_now())))
            return (cursor.rowcount or 0) > 0

    async def votes(self, thread_id: str, plan_hash: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT approver, approved, userids, comment, at FROM alm_approval_vote "
            "WHERE thread_id = ? AND plan_hash = ? ORDER BY at", (thread_id, plan_hash))
        return [{"approver": r[0], "approved": bool(r[1]), "userids": json.loads(r[2]),
                 "comment": r[3], "at": r[4]} for r in rows]
