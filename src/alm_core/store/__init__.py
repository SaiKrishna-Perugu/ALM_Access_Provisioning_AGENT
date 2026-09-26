"""Durable state for the autonomous pipeline.

Two implementations behind one interface: Postgres for anything that writes, and
an in-memory stand-in for shadow mode and local development.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..errors import ConfigError
from ..logging import get_logger
from ..models import ApprovalDecision, ApprovalRequest, AuditEvent, Operation, ProvisionResult
from .memory import MemoryStore
from .postgres import PostgresStore

log = get_logger("alm.store")

__all__ = ["Store", "MemoryStore", "PostgresStore", "get_store"]


@runtime_checkable
class Store(Protocol):
    """What the graph and the API need from persistence."""

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def migrate(self) -> None: ...

    async def claim(self, key: str, *, run_id: str, work_item_id: str, userid: str,
                    operation: Operation) -> tuple[bool, ProvisionResult | None]: ...
    async def complete(self, key: str, result: ProvisionResult) -> None: ...
    async def release(self, key: str) -> None: ...

    async def record(self, event: AuditEvent) -> None: ...
    async def record_many(self, events: list[AuditEvent]) -> None: ...
    async def run_events(self, run_id: str) -> list[dict]: ...
    async def recent_runs(self, limit: int = 50) -> list[dict]: ...

    async def save_approval_request(self, request: ApprovalRequest) -> None: ...
    async def save_approval_decision(self, decision: ApprovalDecision) -> None: ...
    async def get_approval(self, thread_id: str
                           ) -> tuple[ApprovalRequest | None, ApprovalDecision | None]: ...


async def get_store(settings=None) -> Store:
    """Return a started store appropriate to the configuration.

    A configuration that can write but has no database is a configuration error,
    not something to paper over with the in-memory store: without the ledger a
    replayed webhook would create the same contributor twice.
    """
    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    if settings.postgres_dsn:
        store: Store = PostgresStore(settings.postgres_dsn,
                                     pool_min=settings.postgres_pool_min,
                                     pool_max=settings.postgres_pool_max,
                                     settings=settings)
        await store.start()
        await store.migrate()
        return store

    if getattr(settings, "ledger_path", ""):
        from .sqlite import SqliteStore

        store = SqliteStore(settings.ledger_path)
        await store.start()
        await store.migrate()
        return store

    if not settings.shadow_mode:
        raise ConfigError(
            "no ALM_POSTGRES_DSN configured but shadow mode is off. Writes require the "
            "idempotency ledger - set the DSN or run with ALM_SHADOW_MODE=true.")

    log.warning("using_memory_store",
                reason="no postgres dsn; shadow mode performs no writes")
    store = MemoryStore()
    await store.start()
    return store
