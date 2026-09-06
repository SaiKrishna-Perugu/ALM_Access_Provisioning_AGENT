"""Agent memory: what previous runs learned, available to this one.

Without memory each run rediscovers the estate from scratch - that a particular
work-item template always produces malformed New Users rows, that a given user
was already rejected by LDAP last week, that a domain's requests always need
reactivation rather than creation. Re-deriving that every time is not just slow,
it produces inconsistent decisions between runs, which is worse.

Two kinds, deliberately separated:

* **Episodic** - what happened to a specific subject (a user, a work item).
  Scoped and factual. "AB12345 was not found in LDAP on 2026-09-01."
* **Semantic** - a generalisation an agent drew and a later run may reuse.
  "Work items from domain X list users in the Justification field, not New
  Users." These are *hints*, never authority: an agent may act on one only after
  confirming it against a live tool call, because a stale generalisation is
  exactly how a system starts being confidently wrong.

Memory is never a substitute for validation. Nothing here can authorise a write.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from alm_core.logging import get_logger

log = get_logger("alm.memory")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alm_agent_memory (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('episodic', 'semantic')),
    subject     TEXT NOT NULL DEFAULT '',
    tags        TEXT[] NOT NULL DEFAULT '{}',
    content     TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 0.5,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded  BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS alm_memory_subject ON alm_agent_memory (subject, created_at DESC);
CREATE INDEX IF NOT EXISTS alm_memory_tags    ON alm_agent_memory USING GIN (tags);
"""

MAX_CONTENT = 1000
RECALL_LIMIT = 8


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    """Postgres-backed when a DSN is configured, in-process otherwise."""

    def __init__(self, pool_owner=None):
        # ``pool_owner`` is the alm_core PostgresStore; reusing its pool keeps
        # connection accounting in one place.
        self.owner = pool_owner
        self._local: list[dict] = []

    @property
    def durable(self) -> bool:
        return self.owner is not None and getattr(self.owner, "_pool", None) is not None

    async def migrate(self) -> None:
        if not self.durable:
            return
        async with self.owner._conn() as conn, conn.cursor() as cur:
            await cur.execute(SCHEMA_SQL)
        log.info("memory_schema_ready")

    # ---------------------------------------------------------------- write

    async def remember(self, *, kind: str, content: str, subject: str = "",
                       tags: list[str] | None = None, author: str = "",
                       run_id: str = "", confidence: float = 0.5) -> str:
        if kind not in ("episodic", "semantic"):
            return "rejected: kind must be 'episodic' or 'semantic'"
        content = (content or "").strip()[:MAX_CONTENT]
        if not content:
            return "rejected: empty content"
        tags = [t.strip().lower() for t in (tags or []) if t.strip()][:8]
        confidence = max(0.0, min(1.0, float(confidence)))

        if not self.durable:
            self._local.append({"kind": kind, "subject": subject, "tags": tags,
                                "content": content, "author": author, "run_id": run_id,
                                "confidence": confidence, "created_at": _now()})
            return "remembered (in-process only; not durable)"

        async with self.owner._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO alm_agent_memory (kind, subject, tags, content, author, "
                "run_id, confidence) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (kind, subject, tags, content, author, run_id, confidence))
        log.info("memory_written", kind=kind, subject=subject, author=author)
        return "remembered"

    async def supersede(self, subject: str, tags: list[str] | None = None) -> int:
        """Retire memories that a live check has just contradicted.

        Called when an agent discovers a remembered generalisation is wrong. A
        memory store that only accumulates becomes a source of confident errors.
        """
        if not self.durable:
            before = len(self._local)
            self._local = [m for m in self._local if m["subject"] != subject]
            return before - len(self._local)
        async with self.owner._conn() as conn, conn.cursor() as cur:
            if tags:
                await cur.execute(
                    "UPDATE alm_agent_memory SET superseded = true "
                    "WHERE subject = %s AND tags && %s AND NOT superseded",
                    (subject, tags))
            else:
                await cur.execute(
                    "UPDATE alm_agent_memory SET superseded = true "
                    "WHERE subject = %s AND NOT superseded", (subject,))
            return cur.rowcount or 0

    # ----------------------------------------------------------------- read

    async def recall(self, *, subject: str = "", tags: list[str] | None = None,
                     kind: str = "", limit: int = RECALL_LIMIT) -> list[dict]:
        tags = [t.strip().lower() for t in (tags or []) if t.strip()]
        limit = max(1, min(limit, 25))

        if not self.durable:
            rows = [m for m in self._local
                    if (not subject or m["subject"] == subject)
                    and (not kind or m["kind"] == kind)
                    and (not tags or set(tags) & set(m["tags"]))]
            return sorted(rows, key=lambda m: m["created_at"], reverse=True)[:limit]

        clauses = ["NOT superseded"]
        params: list = []
        if subject:
            clauses.append("subject = %s")
            params.append(subject)
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        if tags:
            clauses.append("tags && %s")
            params.append(tags)
        params.append(limit)

        async with self.owner._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT kind, subject, tags, content, author, confidence, created_at "
                f"FROM alm_agent_memory WHERE {' AND '.join(clauses)} "
                "ORDER BY confidence DESC, created_at DESC LIMIT %s", params)
            rows = await cur.fetchall()
        columns = ["kind", "subject", "tags", "content", "author", "confidence",
                   "created_at"]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    async def brief(self, subjects: list[str], tags: list[str] | None = None) -> str:
        """A compact briefing injected into an agent's opening context.

        Capped hard: memory is a hint channel, not a way to smuggle an unbounded
        prompt past the token budget.
        """
        seen: list[dict] = []
        for subject in subjects[:10]:
            seen.extend(await self.recall(subject=subject, limit=3))
        seen.extend(await self.recall(tags=tags or [], kind="semantic", limit=5))

        if not seen:
            return ""
        lines = []
        for item in seen[:12]:
            marker = "fact" if item["kind"] == "episodic" else "hint"
            subject = f"[{item['subject']}] " if item["subject"] else ""
            lines.append(f"- ({marker}) {subject}{item['content']}")
        return ("What previous runs recorded. Facts are scoped observations; hints are "
                "generalisations that MUST be confirmed with a tool call before you act "
                "on them:\n" + "\n".join(lines))


def as_json(rows: list[dict]) -> str:
    return json.dumps(rows, default=str, ensure_ascii=False, indent=2)
