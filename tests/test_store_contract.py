"""One contract, every store: the ledger must behave the same wherever it lives.

A local run on SQLite has to be exactly as safe to repeat as a cloud run on
Postgres. The shared guarantees are tested once, against each implementation;
SQLite-only guarantees (durability across reopen, append-only audit) follow.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("pydantic")

from alm_core.errors import IdempotencyViolation  # noqa: E402
from alm_core.models import (  # noqa: E402
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    Operation,
    Outcome,
    ProvisionResult,
    utcnow,
)
from alm_core.store.memory import MemoryStore  # noqa: E402

KEY = "k-1001-AB12345-jts_create"


def _claim(store, run_id="r1"):
    return store.claim(KEY, run_id=run_id, work_item_id="1001", userid="AB12345",
                       operation=Operation.JTS_CREATE)


def _result(outcome=Outcome.OK):
    return ProvisionResult(userid="AB12345", operation=Operation.JTS_CREATE,
                           outcome=outcome, work_item_id="1001", idempotency_key=KEY,
                           message="created")


async def _age_claim(store, by: timedelta) -> None:
    """Pretend the in-flight claim was taken ``by`` ago."""
    if isinstance(store, MemoryStore):
        store._claims[KEY]["claimed_at"] -= by
        return
    from alm_core.store.sqlite import _ts

    await store._conn().execute("UPDATE alm_idempotency SET claimed_at = ? WHERE key = ?",
                                (_ts(utcnow() - by), KEY))


@pytest.fixture(params=["memory", "sqlite"])
def make_store(request, tmp_path):
    """A factory, so SQLite tests can reopen the same file."""
    if request.param == "sqlite":
        pytest.importorskip("aiosqlite")
        from alm_core.store.sqlite import SqliteStore

        path = str(tmp_path / "ledger.db")

        async def factory():
            store = SqliteStore(path)
            await store.start()
            await store.migrate()
            return store
    else:
        async def factory():
            return MemoryStore()
    return factory


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- ledger

def test_first_claim_proceeds_and_a_completed_write_replays(make_store):
    async def scenario():
        store = await make_store()
        assert await _claim(store) == (True, None)
        await store.complete(KEY, _result())
        proceed, previous = await _claim(store, run_id="r2")
        assert not proceed and previous.message == "created"
        await store.close()
    run(scenario())


def test_a_live_in_flight_claim_blocks_a_second_writer(make_store):
    async def scenario():
        store = await make_store()
        await _claim(store)
        with pytest.raises(IdempotencyViolation):
            await _claim(store, run_id="r2")
        await store.close()
    run(scenario())


def test_a_stale_claim_is_taken_over(make_store):
    async def scenario():
        store = await make_store()
        await _claim(store)
        await _age_claim(store, timedelta(minutes=20))
        assert await _claim(store, run_id="r2") == (True, None)
        await store.close()
    run(scenario())


def test_a_failed_write_may_be_retried(make_store):
    async def scenario():
        store = await make_store()
        await _claim(store)
        await store.complete(KEY, _result(Outcome.FAILED))
        assert await _claim(store, run_id="r2") == (True, None)
        await store.close()
    run(scenario())


def test_release_turns_a_claim_into_a_retryable_failure(make_store):
    async def scenario():
        store = await make_store()
        await _claim(store)
        await store.release(KEY)
        assert await _claim(store, run_id="r2") == (True, None)
        await store.close()
    run(scenario())


# -------------------------------------------------------------- audit/approval

def test_audit_events_come_back_in_order_for_their_run(make_store):
    async def scenario():
        store = await make_store()
        for step in ("a", "b"):
            await store.record(AuditEvent(run_id="r1", step=step, outcome=Outcome.OK,
                                          detail={"n": step}))
        await store.record(AuditEvent(run_id="other", step="x", outcome=Outcome.OK))
        events = await store.run_events("r1")
        assert [e["step"] for e in events] == ["a", "b"]
        assert events[0]["detail"] == {"n": "a"}
        await store.close()
    run(scenario())


def test_a_decision_belongs_to_the_plan_it_was_made_on(make_store):
    async def scenario():
        store = await make_store()
        request = ApprovalRequest(thread_id="t", run_id="r", environment="TEST",
                                  expires_at=utcnow() + timedelta(hours=1), plan_hash="A")
        await store.save_approval_request(request)
        await store.save_approval_decision(ApprovalDecision(
            thread_id="t", approved=True, approver="a", plan_hash="A"))
        await store.save_approval_request(request)            # same plan: kept
        assert (await store.get_approval("t"))[1] is not None
        await store.save_approval_request(request.model_copy(update={"plan_hash": "B"}))
        saved, decision = await store.get_approval("t")
        assert saved.plan_hash == "B" and decision is None   # new plan: undecided
        await store.close()
    run(scenario())


# ------------------------------------------------------------------ sqlite only

@pytest.fixture
def sqlite_path(tmp_path):
    pytest.importorskip("aiosqlite")
    return str(tmp_path / "nested" / "ledger.db")


def test_sqlite_ledger_survives_closing_and_reopening(sqlite_path):
    from alm_core.store.sqlite import SqliteStore

    async def scenario():
        first = SqliteStore(sqlite_path)
        await first.start()
        await first.migrate()
        await _claim(first)
        await first.complete(KEY, _result())
        await first.close()

        second = SqliteStore(sqlite_path)
        await second.start()
        await second.migrate()
        proceed, previous = await _claim(second, run_id="later")
        assert not proceed and previous.outcome == Outcome.OK
        await second.close()
    run(scenario())


def test_sqlite_audit_trail_is_append_only(sqlite_path):
    import sqlite3

    from alm_core.store.sqlite import SqliteStore

    async def scenario():
        store = SqliteStore(sqlite_path)
        await store.start()
        await store.migrate()
        await store.record(AuditEvent(run_id="r1", step="a", outcome=Outcome.OK))
        for statement in ("UPDATE alm_audit SET message = 'edited'", "DELETE FROM alm_audit"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                await store._conn().execute(statement)
        assert len(await store.run_events("r1")) == 1
        await store.close()
    run(scenario())


def test_get_store_uses_sqlite_when_a_ledger_path_is_set(sqlite_path):
    from alm_core.config import Settings
    from alm_core.store import get_store
    from alm_core.store.sqlite import SqliteStore

    async def scenario():
        settings = Settings(_env_file=None, orchestration="deterministic",
                            shadow_mode=False, ledger_path=sqlite_path)
        store = await get_store(settings)
        assert isinstance(store, SqliteStore)
        await store.close()
    run(scenario())


def test_writes_without_any_durable_ledger_are_refused():
    from alm_core.config import Settings

    with pytest.raises(Exception, match="ALM_LEDGER_PATH"):
        Settings(_env_file=None, orchestration="deterministic", shadow_mode=False)


# ------------------------------------------------------------ schema version

def _sqlite_or_skip():
    pytest.importorskip("aiosqlite")
    from alm_core.store import sqlite

    return sqlite


def test_a_new_ledger_records_its_schema_version_once(tmp_path):
    sqlite = _sqlite_or_skip()

    async def scenario():
        store = sqlite.SqliteStore(str(tmp_path / "alm.db"))
        await store.start()
        await store.migrate()
        await store.migrate()  # every run migrates; it must stay one row
        rows = await store._fetchall("SELECT version FROM alm_schema_version")
        version = await store.schema_version()
        await store.close()
        return rows, version

    rows, version = asyncio.run(scenario())
    assert version == sqlite.SCHEMA_VERSION
    assert len(rows) == len(sqlite.MIGRATIONS)


def test_an_older_ledger_is_upgraded_step_by_step(tmp_path, monkeypatch):
    sqlite = _sqlite_or_skip()
    path = str(tmp_path / "alm.db")

    async def open_and_migrate():
        store = sqlite.SqliteStore(path)
        await store.start()
        await store.migrate()
        return store

    async def scenario():
        await (await open_and_migrate()).close()  # a ledger at version 1
        monkeypatch.setattr(sqlite, "MIGRATIONS", sqlite.MIGRATIONS + [
            (2, "ALTER TABLE alm_approval ADD COLUMN note TEXT NOT NULL DEFAULT '';")])
        monkeypatch.setattr(sqlite, "SCHEMA_VERSION", 2)
        store = await open_and_migrate()
        columns = [r[1] for r in await store._fetchall("PRAGMA table_info(alm_approval)")]
        version = await store.schema_version()
        await store.close()
        return columns, version

    columns, version = asyncio.run(scenario())
    assert "note" in columns and version == 2


def test_a_ledger_from_newer_code_is_refused(tmp_path):
    sqlite = _sqlite_or_skip()
    from alm_core.errors import ConfigError

    async def scenario():
        store = sqlite.SqliteStore(str(tmp_path / "alm.db"))
        await store.start()
        await store.migrate()
        await store._conn().execute("INSERT INTO alm_schema_version VALUES (99)")
        try:
            await store.migrate()
        finally:
            await store.close()

    with pytest.raises(ConfigError, match="newer version"):
        asyncio.run(scenario())
