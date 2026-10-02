"""The run registry, job queue, stop requests, leases and traces - on every store.

These are what let several API replicas and workers share the work. The
guarantees that matter: a job runs on one worker at a time, a thread never
runs twice at once, a dead worker's job comes back, and a job that keeps
failing stops being retried.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pydantic")

from conftest import run_async  # noqa: E402

from alm_core.store.memory import MemoryStore  # noqa: E402


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store_factory(request, tmp_path):
    if request.param == "postgres":
        dsn = request.getfixturevalue("postgres_dsn")
        pytest.importorskip("psycopg_pool")
        from alm_core.store.postgres import PostgresStore

        def make():
            return PostgresStore(dsn, pool_min=1, pool_max=4)
    elif request.param == "sqlite":
        pytest.importorskip("aiosqlite")
        from alm_core.store.sqlite import SqliteStore

        def make():
            return SqliteStore(str(tmp_path / "alm.db"))
    else:
        def make():
            return MemoryStore()
    return make


def with_store(factory, scenario):
    async def main():
        store = factory()
        await store.start()
        await store.migrate()
        try:
            return await scenario(store)
        finally:
            await store.close()
    return run_async(main())


async def expire(store, job_id):
    """Pretend the job's lease ran out (its worker died)."""
    if isinstance(store, MemoryStore):
        from datetime import datetime, timedelta, timezone

        store._job(job_id)["locked_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        return
    if type(store).__name__ == "PostgresStore":
        async with store._conn() as conn:
            await conn.execute("UPDATE alm_run_job SET locked_until = now() - interval '1 second' "
                               "WHERE id = %s", (job_id,))
        return
    await store._conn().execute(
        "UPDATE alm_run_job SET locked_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,))


async def make_ready(store, job_id):
    """Skip a retry's backoff."""
    if isinstance(store, MemoryStore):
        from datetime import datetime, timedelta, timezone

        store._job(job_id)["available_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        return
    if type(store).__name__ == "PostgresStore":
        async with store._conn() as conn:
            await conn.execute("UPDATE alm_run_job SET available_at = now() - interval '1 second' "
                               "WHERE id = %s", (job_id,))
        return
    await store._conn().execute(
        "UPDATE alm_run_job SET available_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,))


# ---------------------------------------------------------------- registry

def test_the_run_registry_creates_updates_and_lists(store_factory):
    async def scenario(store):
        await store.upsert_run("wi-1", status="queued", mode="commit", scope=["1001"],
                               requested_by="ops@example.com", trigger="web")
        await store.upsert_run("wi-1", status="done", report={"halted": False, "hops": 4})
        await store.upsert_run("wi-2", status="running")
        with pytest.raises(ValueError):
            await store.upsert_run("wi-1", password="x")  # pragma: allowlist secret
        with pytest.raises(ValueError):
            await store.upsert_run("wi-1", status="exploded")
        return await store.get_run("wi-1"), await store.list_runs(), await store.get_run("nope")

    one, runs, missing = with_store(store_factory, scenario)
    assert one["status"] == "done" and one["mode"] == "commit"
    assert one["scope"] == ["1001"] and one["report"] == {"halted": False, "hops": 4}
    assert one["requested_by"] == "ops@example.com" and one["created_at"]
    assert {r["thread_id"] for r in runs} == {"wi-1", "wi-2"} and missing is None


# ------------------------------------------------------------------- queue

def test_jobs_are_claimed_oldest_first_and_once(store_factory):
    async def scenario(store):
        first = await store.enqueue_job("start", "wi-1", {"work_items": ["1"]})
        second = await store.enqueue_job("start", "wi-2")
        a = await store.claim_job("worker-a", 60)
        b = await store.claim_job("worker-b", 60)
        c = await store.claim_job("worker-c", 60)
        return first, second, a, b, c

    first, second, a, b, c = with_store(store_factory, scenario)
    assert a["id"] == first and a["payload"] == {"work_items": ["1"]} and a["attempts"] == 1
    assert b["id"] == second and c is None


def test_a_thread_never_runs_on_two_workers_at_once(store_factory):
    async def scenario(store):
        await store.enqueue_job("start", "wi-1")
        await store.enqueue_job("resume", "wi-1")
        await store.enqueue_job("start", "wi-2")
        a = await store.claim_job("worker-a", 60)
        b = await store.claim_job("worker-b", 60)   # skips wi-1's resume: wi-1 is held
        c = await store.claim_job("worker-c", 60)
        await store.finish_job(a["id"], "worker-a", ok=True)
        d = await store.claim_job("worker-c", 60)   # now the resume may run
        return a, b, c, d

    a, b, c, d = with_store(store_factory, scenario)
    assert (a["thread_id"], b["thread_id"], c) == ("wi-1", "wi-2", None)
    assert d["thread_id"] == "wi-1" and d["kind"] == "resume"


def test_a_dead_workers_job_is_taken_over(store_factory):
    async def scenario(store):
        job = await store.enqueue_job("start", "wi-1")
        await store.claim_job("worker-a", 60)
        await expire(store, job)
        taken = await store.claim_job("worker-b", 60)
        lost = await store.finish_job(job, "worker-a", ok=True)  # the old owner wakes up
        done = await store.finish_job(job, "worker-b", ok=True)
        return taken, lost, done

    taken, lost, done = with_store(store_factory, scenario)
    assert taken["locked_by"] == "worker-b" and taken["attempts"] == 2
    assert (lost, done) == ("lost", "done")


def test_failures_retry_with_backoff_then_go_dead(store_factory):
    async def scenario(store):
        job = await store.enqueue_job("start", "wi-1")
        outcomes = []
        for attempt in range(3):
            claimed = await store.claim_job("w", 60, max_attempts=3)
            if claimed is None:
                outcomes.append("not ready")
                break
            outcomes.append(await store.finish_job(job, "w", ok=False, error=f"boom {attempt}",
                                                   max_attempts=3, retry_seconds=30))
            # Backoff: not claimable straight away.
            outcomes.append(await store.claim_job("w", 60, max_attempts=3))
            await make_ready(store, job)
        dead = await store.list_jobs(status="dead")
        return outcomes, dead, await store.queue_depth()

    outcomes, dead, depth = with_store(store_factory, scenario)
    assert outcomes == ["queued", None, "queued", None, "dead", None]
    assert dead[0]["error"] == "boom 2" and depth == {"dead": 1}


def test_a_redelivered_trigger_does_not_queue_a_second_run(store_factory):
    async def scenario(store):
        a = await store.enqueue_job("start", "wi-1", dedupe=True)
        b = await store.enqueue_job("start", "wi-1", dedupe=True)
        c = await store.enqueue_job("resume", "wi-1", dedupe=True)
        return a, b, c, await store.queue_depth()

    a, b, c, depth = with_store(store_factory, scenario)
    assert a and b is None and c and depth == {"queued": 2}


def test_a_running_worker_extends_its_lease(store_factory):
    async def scenario(store):
        job = await store.enqueue_job("start", "wi-1")
        await store.claim_job("w", 60)
        return (await store.extend_job(job, "w", 600),
                await store.extend_job(job, "someone-else", 600))

    assert with_store(store_factory, scenario) == (True, False)


def test_postgres_claims_from_many_workers_at_once_never_share_a_thread(postgres_dsn):
    pytest.importorskip("psycopg_pool")
    from alm_core.store.postgres import PostgresStore

    async def scenario():
        store = PostgresStore(postgres_dsn, pool_min=4, pool_max=12)
        await store.start()
        await store.migrate()
        try:
            for n in range(10):
                await store.enqueue_job("start", f"wi-{n % 5}")
            claims = await asyncio.gather(*(store.claim_job(f"w{i}", 60) for i in range(12)))
            return [c for c in claims if c]
        finally:
            await store.close()

    claimed = run_async(scenario())
    threads = [c["thread_id"] for c in claimed]
    assert len(threads) == 5 and len(set(threads)) == 5


# ----------------------------------------------- stop, replay, lease, trace

def test_stop_requests_are_shared_and_cleared(store_factory):
    async def scenario(store):
        before = await store.stop_request("wi-1")
        await store.request_stop("wi-1", "web:ops")
        during = await store.stop_request("wi-1")
        await store.clear_stop("wi-1")
        return before, during, await store.stop_request("wi-1")

    before, during, after = with_store(store_factory, scenario)
    assert before is None and during["by"] == "web:ops" and during["at"] and after is None


def test_a_webhook_delivery_is_accepted_once(store_factory):
    async def scenario(store):
        return [await store.remember_delivery("d-1"), await store.remember_delivery("d-1"),
                await store.remember_delivery("d-2")]

    assert with_store(store_factory, scenario) == [True, False, True]


def test_a_lease_has_one_holder_until_it_expires(store_factory):
    async def scenario(store):
        a = await store.try_lease("scheduler", "a", 60)
        b = await store.try_lease("scheduler", "b", 60)
        a_again = await store.try_lease("scheduler", "a", 60)
        await store.try_lease("short", "a", -1)  # already expired
        b_short = await store.try_lease("short", "b", 60)
        return a, b, a_again, b_short

    assert with_store(store_factory, scenario) == (True, False, True, True)


def test_traces_page_by_cursor_per_thread(store_factory):
    async def scenario(store):
        await store.record_trace("wi-1", [{"seq": 0, "service": "run", "kind": "started"},
                                          {"seq": 1, "service": "tool", "kind": "tool_call"}])
        await store.record_trace("wi-2", [{"seq": 0, "service": "run", "kind": "started"}])
        await store.record_trace("wi-1", [{"seq": 2, "service": "run", "kind": "finished"}])
        everything = await store.trace_since("wi-1")
        tail = await store.trace_since("wi-1", after=everything[1]["cursor"])
        return everything, tail

    everything, tail = with_store(store_factory, scenario)
    assert [r["kind"] for r in everything] == ["started", "tool_call", "finished"]
    assert [r["kind"] for r in tail] == ["finished"]


def test_a_worker_claims_only_the_kinds_it_handles(store_factory):
    """Run workers and the AD worker share one queue without taking each
    other's jobs."""
    async def scenario(store):
        await store.enqueue_job("ad_job", "key-1", {"userid": "AB12345"})
        await store.enqueue_job("start", "wi-1")
        run_job = await store.claim_job("runs", 60, kinds=("start", "resume"))
        nothing = await store.claim_job("runs", 60, kinds=("start", "resume"))
        ad_job = await store.claim_job("ad", 60, kinds=("ad_job",))
        return run_job, nothing, ad_job

    run_job, nothing, ad_job = with_store(store_factory, scenario)
    assert run_job["kind"] == "start" and nothing is None
    assert ad_job["kind"] == "ad_job" and ad_job["payload"] == {"userid": "AB12345"}
