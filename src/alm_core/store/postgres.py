"""Durable state: the idempotency ledger, the audit table and approval records.

The ledger is what makes a replayed webhook safe. Every write is claimed before
it is attempted and completed after it succeeds, so the three interesting cases
are distinguishable rather than conflated:

* **claimed** - nobody has done this; go ahead.
* **completed** - a previous run already did it; return that result, write nothing.
* **in flight** - another worker holds the claim. Wait, unless the claim is
  older than the lease, in which case that worker died mid-write and we take it
  over. A crash between "claim" and "complete" must not wedge the pipeline
  forever, but nor should two workers race.

The audit table is append-only by construction: there is no UPDATE or DELETE
statement in this module for ``alm_audit``, and the migration grants the
application role INSERT and SELECT only.

The database is reached on a private address, and authenticated with the
cloud's **IAM database authentication** (``ALM_DB_AUTH``: Cloud SQL, RDS or
Azure Database for PostgreSQL) - the password is a short-lived token minted
from the runtime identity, so there is no database password to store or rotate.
Tokens expire, so the pool mints one for every new connection rather than
pinning a conninfo string at pool construction.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import ConfigError, IdempotencyViolation
from ..logging import get_logger
from ..models import ApprovalDecision, ApprovalRequest, AuditEvent, Operation, ProvisionResult
from .runs import JOB_SELECT, RUN_SELECT, job_row, next_job_status, run_row, run_values

log = get_logger("alm.store")

# How long a claim is trusted before another worker may take it over.
CLAIM_LEASE = timedelta(minutes=15)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alm_idempotency (
    key             TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    work_item_id    TEXT NOT NULL,
    userid          TEXT NOT NULL,
    operation       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('in_flight', 'completed', 'failed')),
    result          JSONB,
    claimed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS alm_idempotency_run  ON alm_idempotency (run_id);
CREATE INDEX IF NOT EXISTS alm_idempotency_user ON alm_idempotency (userid);

-- Append only. The application role is granted INSERT and SELECT, never
-- UPDATE or DELETE: an audit trail that can be edited is not an audit trail.
CREATE TABLE IF NOT EXISTS alm_audit (
    id              BIGGENERATED_PLACEHOLDER,
    run_id          TEXT NOT NULL,
    thread_id       TEXT NOT NULL DEFAULT '',
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
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
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS alm_audit_run  ON alm_audit (run_id, at);
CREATE INDEX IF NOT EXISTS alm_audit_user ON alm_audit (userid, at);

CREATE TABLE IF NOT EXISTS alm_approval (
    thread_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    environment     TEXT NOT NULL DEFAULT '',
    plan_hash       TEXT NOT NULL,
    request         JSONB NOT NULL,
    decision        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS alm_approval_run ON alm_approval (run_id);
""".replace("BIGGENERATED_PLACEHOLDER", "BIGSERIAL PRIMARY KEY")

# Schema changes are appended here, never edited: (version, SQL to reach it).
# Version 1 is SCHEMA_SQL itself. The same versions as the SQLite ledger.
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
    scope            JSONB NOT NULL DEFAULT '[]'::jsonb,
    requested_by     TEXT NOT NULL DEFAULT '',
    trigger          TEXT NOT NULL DEFAULT '',
    operator_request TEXT NOT NULL DEFAULT '',
    environment      TEXT NOT NULL DEFAULT '',
    version          TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    report           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alm_run_updated ON alm_run (updated_at);

CREATE TABLE IF NOT EXISTS alm_run_job (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,
    thread_id    TEXT NOT NULL,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'dead')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by    TEXT NOT NULL DEFAULT '',
    locked_until TIMESTAMPTZ,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS alm_run_job_ready ON alm_run_job (status, available_at);
CREATE INDEX IF NOT EXISTS alm_run_job_thread ON alm_run_job (thread_id, status);

CREATE TABLE IF NOT EXISTS alm_run_control (
    thread_id    TEXT PRIMARY KEY,
    stop_by      TEXT NOT NULL,
    stop_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alm_webhook_seen (
    delivery_id  TEXT PRIMARY KEY,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alm_lease (
    name         TEXT PRIMARY KEY,
    holder       TEXT NOT NULL,
    until        TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS alm_trace_event (
    id           BIGSERIAL PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    record       JSONB NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alm_trace_thread ON alm_trace_event (thread_id, id);
"""),
    # Version 3: one row per approver per plan, for the two-person rule.
    (3, """
CREATE TABLE IF NOT EXISTS alm_approval_vote (
    thread_id    TEXT NOT NULL,
    plan_hash    TEXT NOT NULL,
    approver     TEXT NOT NULL,
    approved     BOOLEAN NOT NULL,
    userids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    comment      TEXT NOT NULL DEFAULT '',
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, plan_hash, approver)
);
"""),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]

# Serialises job claims across every process sharing the database: a claim is
# a few milliseconds, and serialising it is what guarantees one worker per thread.
_CLAIM_LOCK = 7_243_001


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _iam_pool_class(settings):
    """A psycopg pool that mints a fresh IAM token for each new connection.

    Pinning the token into the pool's conninfo would work for an hour and then
    fail every reconnect after that - the classic Cloud SQL IAM bug. Overriding
    the connect step keeps long-lived pools healthy.
    """
    from psycopg_pool import AsyncConnectionPool

    from ..credentials import postgres_dsn

    class IamAsyncConnectionPool(AsyncConnectionPool):
        async def _connect(self, timeout: float | None = None):
            self.conninfo = postgres_dsn(settings)
            return await super()._connect(timeout=timeout)

    return IamAsyncConnectionPool


class PostgresStore:
    """Async access to the ledger, audit table and approval records."""

    def __init__(self, dsn: str, *, pool_min: int = 1, pool_max: int = 10,
                 settings=None):
        if not dsn:
            raise ConfigError("PostgresStore needs ALM_POSTGRES_DSN")
        self.dsn = dsn
        self.pool_min = pool_min
        self.pool_max = pool_max
        self.settings = settings
        self._pool = None

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._pool is not None:
            return
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError as err:  # pragma: no cover
            raise ConfigError(
                "psycopg[binary,pool] is required for PostgresStore "
                "(pip install -r requirements-cloud.txt)") from err

        iam = bool(self.settings is not None
                   and getattr(self.settings, "database_auth", "password") != "password")
        pool_class = _iam_pool_class(self.settings) if iam else AsyncConnectionPool
        self._pool = pool_class(
            self.dsn, min_size=self.pool_min, max_size=self.pool_max, open=False,
            # Shorter than the IAM token lifetime, so connections recycle before
            # anything server-side decides the credential is stale.
            max_lifetime=30 * 60)
        await self._pool.open(wait=True)
        log.info("store_connected", pool_max=self.pool_max, iam_auth=iam)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def migrate(self) -> None:
        """Create the tables, then bring the schema up to this code's version.

        Safe on every start-up and from several processes at once: an advisory
        lock lets one migrate while the others wait. A database written by newer
        code is refused rather than misread.
        """
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CLAIM_LOCK + 1,))
            await cur.execute(SCHEMA_SQL)
            await cur.execute("CREATE TABLE IF NOT EXISTS alm_schema_version "
                              "(version INTEGER NOT NULL)")
            await cur.execute("SELECT MAX(version) FROM alm_schema_version")
            row = await cur.fetchone()
            current = row[0] if row and row[0] is not None else 0
            if current > SCHEMA_VERSION:
                raise ConfigError(
                    f"the database was written by a newer version of this service "
                    f"(schema {current}; this code knows {SCHEMA_VERSION}). Deploy the "
                    "newer version, or restore a backup taken before it ran.")
            for version, statements in MIGRATIONS:
                if version > current:
                    if statements:
                        await cur.execute(statements)
                    await cur.execute("INSERT INTO alm_schema_version (version) VALUES (%s)",
                                      (version,))
                    log.info("store_schema_migrated", version=version)
        log.info("store_migrated", version=SCHEMA_VERSION)

    async def schema_version(self) -> int:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT MAX(version) FROM alm_schema_version")
            row = await cur.fetchone()
        return row[0] if row and row[0] is not None else 0

    def _conn(self):
        if self._pool is None:
            raise ConfigError("PostgresStore.start() has not been awaited")
        return self._pool.connection()

    # --------------------------------------------------------- idempotency

    async def claim(self, key: str, *, run_id: str, work_item_id: str, userid: str,
                    operation: Operation) -> tuple[bool, ProvisionResult | None]:
        """Try to claim a write.

        Returns ``(True, None)`` when the caller owns the write and must proceed,
        or ``(False, result)`` when it is already completed - ``result`` is the
        original outcome, to be reported as a replay rather than repeated.
        """
        now = datetime.now(timezone.utc)
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO alm_idempotency
                       (key, run_id, work_item_id, userid, operation, status, claimed_at)
                VALUES (%s, %s, %s, %s, %s, 'in_flight', %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, run_id, work_item_id, userid, operation.value, now),
            )
            if cur.rowcount == 1:
                return True, None

            await cur.execute(
                "SELECT status, result, claimed_at, run_id FROM alm_idempotency WHERE key = %s",
                (key,))
            row = await cur.fetchone()
            if row is None:  # deleted between the two statements; treat as ours
                return True, None
            status, result, claimed_at, owner = row

            if status == "completed":
                log.info("idempotent_replay", key=key, userid=userid,
                         operation=operation.value, original_run=owner)
                return False, ProvisionResult.model_validate(result) if result else None

            if status == "in_flight" and now - claimed_at > CLAIM_LEASE:
                # The previous owner died mid-write. Take the claim over rather
                # than leaving the user unprovisioned forever.
                await cur.execute(
                    "UPDATE alm_idempotency SET run_id = %s, claimed_at = %s "
                    "WHERE key = %s AND status = 'in_flight'",
                    (run_id, now, key))
                log.warning("idempotency_claim_taken_over", key=key,
                            previous_run=owner, stale_since=claimed_at.isoformat())
                return True, None

            if status == "in_flight":
                raise IdempotencyViolation(
                    "another worker is performing this write",
                    context={"key": key, "owner_run": owner, "userid": userid,
                             "operation": operation.value})

        # status == 'failed': a previous attempt failed and may be retried.
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE alm_idempotency SET status = 'in_flight', run_id = %s, "
                "claimed_at = %s, completed_at = NULL WHERE key = %s",
                (run_id, now, key))
        return True, None

    async def complete(self, key: str, result: ProvisionResult) -> None:
        """Record the final outcome of a claimed write."""
        # An outcome nobody can confirm is closed, not retried: see OutcomeUnknown.
        status = ("completed" if result.succeeded
                  or result.detail.get("outcome_unknown") else "failed")
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE alm_idempotency SET status = %s, result = %s::jsonb, "
                "completed_at = now() WHERE key = %s",
                (status, _json(result.model_dump(mode="json")), key))

    async def release(self, key: str) -> None:
        """Give up a claim without completing it (e.g. the run was cancelled)."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE alm_idempotency SET status = 'failed', completed_at = now() "
                "WHERE key = %s AND status = 'in_flight'", (key,))

    # --------------------------------------------------------------- audit

    async def record(self, event: AuditEvent) -> None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO alm_audit (run_id, thread_id, at, environment, step, userid,
                                       work_item_id, operation, outcome, idempotency_key,
                                       approver, message, error_type, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (event.run_id, event.thread_id, event.at, event.environment, event.step,
                 event.userid, event.work_item_id,
                 event.operation.value if event.operation else None,
                 event.outcome.value, event.idempotency_key, event.approver,
                 event.message, event.error_type, _json(event.detail)),
            )

    async def record_many(self, events: list[AuditEvent]) -> None:
        for event in events:
            await self.record(event)

    async def run_events(self, run_id: str) -> list[dict]:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, thread_id, at, environment, step, userid, work_item_id, "
                "operation, outcome, idempotency_key, approver, message, error_type, detail "
                "FROM alm_audit WHERE run_id = %s ORDER BY at, id", (run_id,))
            rows = await cur.fetchall()
        columns = ["run_id", "thread_id", "at", "environment", "step", "userid",
                   "work_item_id", "operation", "outcome", "idempotency_key", "approver",
                   "message", "error_type", "detail"]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    async def recent_runs(self, limit: int = 50) -> list[dict]:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, min(at) AS started, max(at) AS last_event, count(*) AS events, "
                "count(*) FILTER (WHERE outcome = 'failed') AS failures "
                "FROM alm_audit GROUP BY run_id ORDER BY started DESC LIMIT %s", (limit,))
            rows = await cur.fetchall()
        return [{"run_id": r[0], "started": r[1], "last_event": r[2],
                 "events": r[3], "failures": r[4]} for r in rows]

    # ------------------------------------------------------------ approval

    async def save_approval_request(self, request: ApprovalRequest) -> None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO alm_approval (thread_id, run_id, environment, plan_hash,
                                          request, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE
                   SET request = EXCLUDED.request, plan_hash = EXCLUDED.plan_hash,
                       expires_at = EXCLUDED.expires_at,
                       -- A decision belongs to the plan it was made on; a new plan
                       -- on the same thread starts undecided. (Right-hand sides
                       -- read the pre-update row.)
                       decision = CASE WHEN alm_approval.plan_hash = EXCLUDED.plan_hash
                                       THEN alm_approval.decision END,
                       decided_at = CASE WHEN alm_approval.plan_hash = EXCLUDED.plan_hash
                                         THEN alm_approval.decided_at END
                """,
                (request.thread_id, request.run_id, request.environment, request.plan_hash,
                 _json(request.model_dump(mode="json")), request.created_at,
                 request.expires_at),
            )

    async def save_approval_decision(self, decision: ApprovalDecision) -> None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE alm_approval SET decision = %s::jsonb, decided_at = %s "
                "WHERE thread_id = %s",
                (_json(decision.model_dump(mode="json")), decision.decided_at,
                 decision.thread_id))

    async def get_approval(self, thread_id: str
                           ) -> tuple[ApprovalRequest | None, ApprovalDecision | None]:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT request, decision FROM alm_approval WHERE thread_id = %s",
                (thread_id,))
            row = await cur.fetchone()
        if not row:
            return None, None
        request = ApprovalRequest.model_validate(row[0]) if row[0] else None
        decision = ApprovalDecision.model_validate(row[1]) if row[1] else None
        return request, decision

    # ---------------------------------------------------------------- runs

    async def upsert_run(self, thread_id: str, **fields) -> None:
        """Create or update a run's registry row. Unknown fields are refused."""
        values = run_values(fields)
        columns = list(values)
        placeholders = ", ".join(
            "%s::jsonb" if c in ("scope", "report") else "%s" for c in columns)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
        sql = (f"INSERT INTO alm_run (thread_id{''.join(', ' + c for c in columns)}) "  # noqa: S608 - columns come from RUN_COLUMNS, never input
               f"VALUES (%s{', ' + placeholders if columns else ''}) "
               "ON CONFLICT (thread_id) DO UPDATE SET "
               f"{updates + ', ' if updates else ''}updated_at = now()")
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(sql, (thread_id, *values.values()))

    async def get_run(self, thread_id: str) -> dict | None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT {', '.join(RUN_SELECT)} FROM alm_run WHERE thread_id = %s",  # noqa: S608 - fixed column list
                (thread_id,))
            row = await cur.fetchone()
        return run_row(row) if row else None

    async def list_runs(self, limit: int = 50) -> list[dict]:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT {', '.join(RUN_SELECT)} FROM alm_run "  # noqa: S608 - fixed column list
                "ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = await cur.fetchall()
        return [run_row(r) for r in rows]

    async def tokens_since(self, since: datetime) -> int:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(SUM((report->'metrics'->>'tokens')::bigint), 0) "
                "FROM alm_run WHERE created_at >= %s AND report IS NOT NULL", (since,))
            row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    # --------------------------------------------------------------- queue

    async def enqueue_job(self, kind: str, thread_id: str, payload: dict | None = None,
                          *, dedupe: bool = False, delay_seconds: float = 0) -> int | None:
        """Queue a job. With ``dedupe``, an unfinished job of the same kind for the
        same thread makes this a no-op (a redelivered webhook, say): returns None."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CLAIM_LOCK,))
            if dedupe:
                await cur.execute(
                    "SELECT 1 FROM alm_run_job WHERE thread_id = %s AND kind = %s "
                    "AND status IN ('queued', 'running')", (thread_id, kind))
                if await cur.fetchone():
                    return None
            await cur.execute(
                "INSERT INTO alm_run_job (kind, thread_id, payload, available_at) "
                "VALUES (%s, %s, %s::jsonb, now() + make_interval(secs => %s)) RETURNING id",
                (kind, thread_id, _json(payload or {}), float(delay_seconds)))
            return (await cur.fetchone())[0]

    async def claim_job(self, worker: str, lease_seconds: float,
                        *, max_attempts: int = 5,
                        kinds: tuple[str, ...] | None = None) -> dict | None:
        """Take the oldest ready job, never one whose thread another worker holds.

        Claims are serialised by a transaction-scoped advisory lock, so two
        workers can never pick two jobs of one thread at the same moment.
        """
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_CLAIM_LOCK,))
            await cur.execute(
                "UPDATE alm_run_job SET status = 'dead', finished_at = now(), "
                "error = 'gave up after ' || attempts || ' attempt(s)' "
                "WHERE attempts >= %s AND (status = 'queued' OR "
                "(status = 'running' AND locked_until < now()))", (max_attempts,))
            await cur.execute(
                f"SELECT {', '.join(JOB_SELECT)} FROM alm_run_job j "  # noqa: S608 - fixed column list
                "WHERE ((j.status = 'queued' AND j.available_at <= now()) "
                "    OR (j.status = 'running' AND j.locked_until < now())) "
                "AND NOT EXISTS (SELECT 1 FROM alm_run_job o WHERE o.thread_id = j.thread_id "
                "    AND o.status = 'running' AND o.locked_until >= now() AND o.id <> j.id) "
                "AND (%s::text[] IS NULL OR j.kind = ANY(%s::text[])) "
                "ORDER BY j.id LIMIT 1", (list(kinds) if kinds else None,
                                          list(kinds) if kinds else None))
            row = await cur.fetchone()
            if row is None:
                return None
            job = job_row(row)
            await cur.execute(
                "UPDATE alm_run_job SET status = 'running', locked_by = %s, "
                "locked_until = now() + make_interval(secs => %s), attempts = attempts + 1 "
                "WHERE id = %s", (worker, float(lease_seconds), job["id"]))
        job.update(status="running", locked_by=worker, attempts=job["attempts"] + 1)
        return job

    async def extend_job(self, job_id: int, worker: str, lease_seconds: float) -> bool:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE alm_run_job SET locked_until = now() + make_interval(secs => %s) "
                "WHERE id = %s AND locked_by = %s AND status = 'running'",
                (float(lease_seconds), job_id, worker))
            return cur.rowcount > 0

    async def finish_job(self, job_id: int, worker: str, *, ok: bool, error: str = "",
                         max_attempts: int = 5, retry_seconds: float = 60) -> str:
        """Close a job. A failure is retried later, with backoff, until
        ``max_attempts``; then it is ``dead`` and waits for a human."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT attempts FROM alm_run_job WHERE id = %s AND locked_by = %s "
                "AND status = 'running' FOR UPDATE", (job_id, worker))
            row = await cur.fetchone()
            if row is None:
                return "lost"  # the lease expired and another worker took it
            status = next_job_status(ok, row[0], max_attempts)
            await cur.execute(
                "UPDATE alm_run_job SET status = %s, error = %s, locked_until = NULL, "
                "available_at = now() + make_interval(secs => %s), "
                "finished_at = CASE WHEN %s = 'queued' THEN NULL ELSE now() END "
                "WHERE id = %s",
                (status, error[:2000], float(retry_seconds * row[0]), status, job_id))
        return status

    async def queue_depth(self) -> dict:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT status, COUNT(*) FROM alm_run_job GROUP BY status")
            return dict(await cur.fetchall())

    async def list_jobs(self, *, status: str = "", limit: int = 100) -> list[dict]:
        async with self._conn() as conn, conn.cursor() as cur:
            if status:
                await cur.execute(
                    f"SELECT {', '.join(JOB_SELECT)} FROM alm_run_job WHERE status = %s "  # noqa: S608 - fixed column list
                    "ORDER BY id DESC LIMIT %s", (status, limit))
            else:
                await cur.execute(
                    f"SELECT {', '.join(JOB_SELECT)} FROM alm_run_job "  # noqa: S608 - fixed column list
                    "ORDER BY id DESC LIMIT %s", (limit,))
            rows = await cur.fetchall()
        return [job_row(r) for r in rows]

    # ------------------------------------------------------------- control

    async def request_stop(self, thread_id: str, by: str) -> None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO alm_run_control (thread_id, stop_by) VALUES (%s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET stop_by = EXCLUDED.stop_by, "
                "stop_at = now()", (thread_id, by))

    async def stop_request(self, thread_id: str) -> dict | None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT stop_by, stop_at FROM alm_run_control WHERE thread_id = %s",
                (thread_id,))
            row = await cur.fetchone()
        return {"by": row[0], "at": row[1].isoformat()} if row else None

    async def clear_stop(self, thread_id: str) -> None:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM alm_run_control WHERE thread_id = %s", (thread_id,))

    # ------------------------------------------------- replay and leadership

    async def remember_delivery(self, delivery_id: str, *,
                                ttl_seconds: float = 86400) -> bool:
        """True the first time a webhook delivery id is seen, False on a replay."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM alm_webhook_seen WHERE seen_at < now() - make_interval(secs => %s)",
                (float(ttl_seconds),))
            await cur.execute(
                "INSERT INTO alm_webhook_seen (delivery_id) VALUES (%s) "
                "ON CONFLICT (delivery_id) DO NOTHING", (delivery_id,))
            return cur.rowcount == 1

    async def try_lease(self, name: str, holder: str, seconds: float) -> bool:
        """Hold (or renew) a named lease. Exactly one holder at a time."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO alm_lease (name, holder, until) "
                "VALUES (%s, %s, now() + make_interval(secs => %s)) "
                "ON CONFLICT (name) DO UPDATE SET holder = EXCLUDED.holder, "
                "until = EXCLUDED.until "
                "WHERE alm_lease.holder = EXCLUDED.holder OR alm_lease.until < now()",
                (name, holder, float(seconds)))
            await cur.execute("SELECT holder FROM alm_lease WHERE name = %s", (name,))
            row = await cur.fetchone()
        return bool(row and row[0] == holder)

    # --------------------------------------------------------------- trace

    async def record_trace(self, thread_id: str, records: list[dict]) -> None:
        if not records:
            return
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO alm_trace_event (thread_id, seq, record) VALUES (%s, %s, %s::jsonb)",
                [(thread_id, int(r.get("seq", 0)), _json(r)) for r in records])

    async def trace_since(self, thread_id: str, after: int = 0,
                          limit: int = 1000) -> list[dict]:
        """Records after cursor ``after``; each carries its ``cursor``."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT id, record FROM alm_trace_event WHERE thread_id = %s AND id > %s "
                "ORDER BY id LIMIT %s", (thread_id, after, limit))
            rows = await cur.fetchall()
        return [{**record, "cursor": cursor} for cursor, record in rows]

    # --------------------------------------------------------------- votes

    async def add_vote(self, thread_id: str, plan_hash: str, approver: str, *,
                       approved: bool, userids: list[str], comment: str = "") -> bool:
        """Record one approver's vote on one plan. False if they already voted."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO alm_approval_vote (thread_id, plan_hash, approver, approved, "
                "userids, comment) VALUES (%s, %s, %s, %s, %s::jsonb, %s) "
                "ON CONFLICT (thread_id, plan_hash, approver) DO NOTHING",
                (thread_id, plan_hash, approver.lower(), approved, _json(userids),
                 comment[:500]))
            return cur.rowcount == 1

    async def votes(self, thread_id: str, plan_hash: str) -> list[dict]:
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT approver, approved, userids, comment, at FROM alm_approval_vote "
                "WHERE thread_id = %s AND plan_hash = %s ORDER BY at", (thread_id, plan_hash))
            rows = await cur.fetchall()
        return [{"approver": r[0], "approved": r[1], "userids": r[2], "comment": r[3],
                 "at": r[4].isoformat()} for r in rows]
