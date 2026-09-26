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

Cloud SQL is reached on the instance private IP over Direct VPC egress, and
authenticated with **IAM database authentication** - the password is a
short-lived access token minted from the runtime service account, so there is no
database password to store or rotate. Tokens expire in an hour, so the pool
re-mints one for every new connection rather than pinning a conninfo string at
pool construction.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import ConfigError, IdempotencyViolation
from ..logging import get_logger
from ..models import ApprovalDecision, ApprovalRequest, AuditEvent, Operation, ProvisionResult

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
                   and getattr(self.settings, "postgres_iam_auth", False))
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
        """Create the tables if they are absent. Safe to run on every startup."""
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
        log.info("store_migrated")

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
