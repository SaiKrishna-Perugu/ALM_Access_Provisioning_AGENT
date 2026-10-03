"""Run workers: queued runs, parked approvals, stops from anywhere, takeovers.

Workers here share one SQLite store (Postgres in production): the queue,
the run registry, stop requests and traces all go through it, exactly as
between machines.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

from test_agentic_sandbox import ScriptedLLM  # noqa: E402
from test_local_runner import local_settings  # noqa: E402

from alm_agents import worker as worker_module  # noqa: E402
from alm_agents.graph import Services, checkpointer_for  # noqa: E402
from alm_agents.memory import MemoryStore  # noqa: E402
from alm_agents.sandbox import SandboxBackend, SandboxEstate  # noqa: E402
from alm_agents.worker import Worker, request_stop, submit_decision, submit_run  # noqa: E402
from alm_core import trace as core_trace  # noqa: E402
from alm_core.models import ApprovalDecision  # noqa: E402
from alm_core.store import get_store  # noqa: E402

PLAN = ["triage", "validator", "risk_officer", "provisioner", "DONE"]


def scripts(userid="AB12345"):
    return {
        "triage": [[("fetch_open_requests", {"limit": 10})]],
        "validator": [[("classify_user", {"userid": userid})]],
        "risk_officer": [[("request_human_approval",
                           {"reason": f"import {userid}", "userids": [userid]})]],
        "provisioner": [[("provision_jts_user", {"userid": userid})]],
    }


class Router:
    """One model client for the process, like a real one. Each run's script
    is chosen by the trace its worker task bound - i.e. by the run."""

    def __init__(self, make):
        self.make = make
        self.scripts: dict[str, ScriptedLLM] = {}
        self.slow_after_stop = None   # an asyncio.Event: model calls hang once set

    def bind_tools(self, _tools, **_kw):
        return self

    async def ainvoke(self, messages, **kw):
        if self.slow_after_stop is not None and self.slow_after_stop.is_set():
            await asyncio.sleep(60)   # abandoned by the stop
        sink = core_trace.current_sink()
        thread = getattr(getattr(sink, "__self__", None), "thread_id", "default")
        if thread not in self.scripts:
            self.scripts[thread] = self.make(thread)
        return await self.scripts[thread].ainvoke(messages, **kw)


class Harness:
    def __init__(self, settings, store, checkpointer, router, estate):
        self.settings, self.store, self.estate = settings, store, estate
        self.cards = []

        async def notifier(request):
            self.cards.append(request)

        self.services = Services(settings=settings, store=store, client=None,
                                 checkpointer=checkpointer, agent_llm=router,
                                 supervisor_llm=router, notifier=notifier,
                                 write_limit=asyncio.Semaphore(4))

    def worker(self, name, **kw):
        return Worker(self.services, worker_id=name, backend=SandboxBackend(self.estate),
                      lease_seconds=kw.pop("lease_seconds", 60), poll_seconds=0.05,
                      trace_dir=kw.pop("trace_dir", str(self.settings.ledger_path) + "-traces"),
                      **kw)


def run_with(tmp_path, scenario, *, make=None, commit=True):
    settings = local_settings(tmp_path, commit=commit)
    settings = settings.model_copy(update={"reconcile_interval_minutes": 0})

    async def main():
        store = await get_store(settings)
        await MemoryStore(store).migrate()
        try:
            async with checkpointer_for(settings) as checkpointer:
                router = Router(make or (lambda _t: ScriptedLLM(PLAN, scripts())))
                harness = Harness(settings, store, checkpointer, router,
                                  SandboxEstate.default())
                harness.router = router
                return await scenario(harness)
        finally:
            await store.close()

    return asyncio.run(main())


async def start(h, thread="wi-1001", items=("1001",), mode="commit"):
    return await submit_run(h.store, thread_id=thread, work_item_ids=list(items), mode=mode,
                            requested_by="test", trigger="web", environment="TEST")


# --------------------------------------------------------------------- runs

def test_a_run_parks_at_approval_and_a_queued_decision_finishes_it(tmp_path):
    async def scenario(h):
        await start(h)
        await h.worker("w1").drain()
        parked = await h.store.get_run("wi-1001")
        request, _ = await h.store.get_approval("wi-1001")
        decision = ApprovalDecision(thread_id="wi-1001", approved=True, approver="web:boss",
                                    plan_hash=request.plan_hash,
                                    approved_userids=["AB12345"])
        await submit_decision(h.store, "wi-1001", decision)
        await h.worker("w2").drain()   # any worker may resume it
        return parked, await h.store.get_run("wi-1001"), h.cards, \
            await h.store.trace_since("wi-1001"), await h.store.queue_depth()

    parked, done, cards, trace, depth = run_with(tmp_path, scenario)
    assert parked["status"] == "awaiting_approval" and parked["run_id"]
    assert len(cards) == 1 and "AB12345" in [i.userid for i in cards[0].items]
    assert done["status"] == "done", done["error"]
    written = {(r["userid"], r["operation"], r["outcome"]) for r in done["report"]["results"]}
    assert ("AB12345", "jts_create", "ok") in written
    assert done["report"]["approval_rounds"] == 1
    kinds = {(r["service"], r["kind"]) for r in trace}
    assert {("run", "job_start"), ("run", "parked"), ("run", "job_resume"),
            ("run", "finished"), ("supervisor", "supervisor")} <= kinds
    assert depth == {"done": 2}


def test_a_dry_run_from_the_queue_writes_nothing(tmp_path):
    async def scenario(h):
        await start(h, mode="dry")
        await h.worker("w1").drain()
        return await h.store.get_run("wi-1001"), h.estate

    run, estate = run_with(tmp_path, scenario)
    # A dry run's card is a preview: nobody votes, the plan carries on to the end.
    assert run["status"] == "done", run["error"]
    assert estate.ad_requests == [] and estate.comments["1001"] == []
    assert all(r["outcome"] != "ok" for r in run["report"]["results"])


def test_a_redelivered_trigger_does_not_start_a_second_run(tmp_path):
    async def scenario(h):
        first = await start(h)
        second = await start(h)
        return first, second, await h.store.queue_depth()

    first, second, depth = run_with(tmp_path, scenario)
    assert first and second is None and depth == {"queued": 1}


def test_a_deployment_that_does_not_write_refuses_a_writing_run(tmp_path):
    async def scenario(h):
        await start(h, mode="commit")
        await h.worker("w1").drain()
        return await h.store.get_run("wi-1001"), await h.store.list_jobs(status="dead")

    run, dead = run_with(tmp_path, scenario, commit=False)
    assert run["status"] == "failed" and "does not write" in run["error"]
    assert len(dead) == 1  # a configuration error is not retried


# -------------------------------------------------------------------- stops

def test_a_stopped_queued_run_never_starts(tmp_path):
    async def scenario(h):
        await start(h)
        status = await request_stop(h.store, "wi-1001", "web:ops")
        await h.worker("w1").drain()
        return status, await h.store.get_run("wi-1001"), h.estate

    status, run, estate = run_with(tmp_path, scenario)
    assert status == "stopping" and run["status"] == "stopped"
    assert "web:ops" in run["error"] and estate.ad_requests == []


def test_a_stop_on_a_parked_run_ends_it_without_writing(tmp_path):
    async def scenario(h):
        await start(h)
        await h.worker("w1").drain()
        await request_stop(h.store, "wi-1001", "web:ops")
        await h.worker("w2").drain()
        return await h.store.get_run("wi-1001")

    run = run_with(tmp_path, scenario)
    assert run["status"] == "stopped", run
    assert run["report"]["halt_reason"] == "stopped by web:ops"
    assert run["report"]["results"] == []


def test_a_stop_from_another_replica_ends_a_running_run_after_its_step(tmp_path):
    """The stop is a row in the store; the worker holding the run reads it."""
    async def scenario(h):
        h.router.slow_after_stop = asyncio.Event()
        await start(h)
        w = h.worker("w1")

        def on_event(thread, kind, data):
            if kind == "supervisor" and data.get("next") == "validator":
                h.router.slow_after_stop.set()
                asyncio.get_running_loop().create_task(
                    request_stop(h.store, thread, "web:other-replica"))

        w.on_event = on_event
        await asyncio.wait_for(w.drain(), timeout=30)
        return await h.store.get_run("wi-1001")

    run = run_with(tmp_path, scenario)
    assert run["status"] == "stopped" and "other-replica" in run["report"]["halt_reason"]


# ---------------------------------------------------------------- takeovers

def test_a_killed_workers_run_continues_elsewhere_without_repeating_a_write(tmp_path):
    async def scenario(h):
        await start(h)
        await h.worker("w1").drain()           # parked at approval
        request, _ = await h.store.get_approval("wi-1001")
        await submit_decision(h.store, "wi-1001", ApprovalDecision(
            thread_id="wi-1001", approved=True, approver="web:boss",
            plan_hash=request.plan_hash, approved_userids=["AB12345"]))

        # Worker A resumes the run and dies right after its first write.
        doomed = h.worker("doomed", lease_seconds=60)
        wrote = asyncio.Event()
        doomed.on_event = lambda _t, kind, data: (
            wrote.set() if kind == "tool_call" and data["tool"] == "provision_jts_user"
            else None)
        job = await h.store.claim_job("doomed", 60)
        task = asyncio.create_task(doomed.handle(job))
        await asyncio.wait_for(wrote.wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The interrupted hop runs again on the survivor; a model asked again
        # asks for the same write (a scripted one needs telling).
        h.router.scripts["wi-1001"] = ScriptedLLM(["DONE"], {
            "provisioner": [[("provision_jts_user", {"userid": "AB12345"})]]})
        # Its lease runs out; worker B claims the same job and continues.
        await h.store._conn().execute(
            "UPDATE alm_run_job SET locked_until = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job["id"],))
        await h.worker("survivor").drain()
        return (await h.store.get_run("wi-1001"),
                await h.store._fetchall(
                    "SELECT status FROM alm_idempotency WHERE userid = 'AB12345' "
                    "AND operation = 'jts_create'"),
                h.estate)

    run, ledger, estate = run_with(tmp_path, scenario)
    assert run["status"] == "done", run["error"]
    assert ledger == [("completed",)]          # written exactly once
    created = [r for r in run["report"]["results"]
               if r["userid"] == "AB12345" and r["operation"] == "jts_create"]
    # The repeated attempt was answered from the ledger, not written again.
    assert created and all(r["outcome"] == "ok" and r["replayed"] for r in created)
    assert estate.ad_requests == []


def test_only_the_lease_holder_schedules_the_sweep(tmp_path):
    async def scenario(h):
        a, b = h.worker("a"), h.worker("b")
        a.reconcile_minutes = b.reconcile_minutes = 60
        for w in (a, b, a, b):
            await w.schedule()
        return await h.store.list_runs(), await h.store.queue_depth()

    runs, depth = run_with(tmp_path, scenario)
    sweeps = [r for r in runs if r["thread_id"].startswith("reconcile-")]
    assert len(sweeps) == 1 and sweeps[0]["requested_by"] == "scheduler"
    assert depth == {"queued": 1}


def test_concurrent_runs_in_one_worker_keep_separate_traces(tmp_path):
    def make(thread):
        user = "AB12345" if thread == "wi-1001" else "TB22322"
        plan = ["triage", "extractor", "validator", "DONE"]
        return ScriptedLLM(plan, {
            "triage": [[("fetch_open_requests", {"limit": 10})]],
            "extractor": [[("recover_user_ids", {"work_item_id": "1002"})]],
            "validator": [[("classify_user", {"userid": user})]]})

    async def scenario(h):
        await start(h, "wi-1001", ("1001",), mode="dry")
        await start(h, "wi-1002", ("1002",), mode="dry")
        await h.worker("w", concurrency=2).drain()
        return (await h.store.trace_since("wi-1001"), await h.store.trace_since("wi-1002"))

    a, b = run_with(tmp_path, scenario, make=make)
    users_a = {r.get("args", {}).get("userid") for r in a if r["service"] == "tool"}
    users_b = {r.get("args", {}).get("userid") for r in b if r["service"] == "tool"}
    assert "AB12345" in users_a and "AB12345" not in users_b
    assert "TB22322" in users_b and "TB22322" not in users_a
    assert all(r["thread_id"] == "wi-1001" for r in a)


def test_the_worker_program_starts_its_services_and_stops_cleanly(tmp_path):
    """`python -m alm_agents.worker`: build services, run, stop on a signal."""
    settings = local_settings(tmp_path, orchestration="deterministic").model_copy(
        update={"reconcile_interval_minutes": 0})

    async def main():
        stopping = asyncio.Event()
        stopping.set()          # as SIGTERM would, straight away
        await asyncio.wait_for(worker_module.serve(settings, stopping=stopping), 30)

    asyncio.run(main())
    assert (tmp_path / "local" / "alm.db").exists()   # the store was opened and migrated


def test_a_synthetic_check_queues_a_dry_run(tmp_path):
    from test_local_runner import local_settings

    from alm_agents.worker import main, queue_synthetic

    settings = local_settings(tmp_path)

    async def scenario():
        thread = await queue_synthetic("1001", settings)
        store = await get_store(settings)
        try:
            return thread, await store.get_run(thread), await store.list_jobs(status="queued")
        finally:
            await store.close()

    thread, run, jobs = asyncio.run(scenario())
    assert thread.startswith("synthetic-1001-")
    assert run["mode"] == "dry" and run["scope"] == ["1001"] and run["trigger"] == "synthetic"
    assert [j["thread_id"] for j in jobs] == [thread]
    assert main(["synthetic", "not-a-number"]) == 2
