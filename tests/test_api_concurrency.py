"""Runs that share a process must not share a run.

The cloud API used to build one graph and one tool context at start-up and
drive every run through them: the blackboard, the policy budgets and the
approval were the process's, not the run's. ``build_services`` /
``run_session`` split what is shared (connections, clients, the store) from
what belongs to one run.
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

from test_agentic_sandbox import ScriptedLLM  # noqa: E402
from test_local_runner import local_settings  # noqa: E402

from alm_agents.graph import Services, checkpointer_for, run_session  # noqa: E402
from alm_agents.memory import MemoryStore  # noqa: E402
from alm_agents.runner import Console, drive  # noqa: E402
from alm_agents.sandbox import SandboxBackend, SandboxEstate  # noqa: E402
from alm_core.models import ApprovalDecision  # noqa: E402
from alm_core.store import get_store  # noqa: E402

_current: contextvars.ContextVar[ScriptedLLM] = contextvars.ContextVar("scripted")


class PerRunLLM:
    """One model client shared by the process, as a real one is. Each run's
    script is found through a context variable set in that run's own task."""

    def bind_tools(self, _tools, **_kw):
        return self

    async def ainvoke(self, messages, **kw):
        await asyncio.sleep(0)  # let the other run interleave
        return await _current.get().ainvoke(messages, **kw)


def script(work_item: str, userid: str) -> ScriptedLLM:
    return ScriptedLLM(["triage", "extractor", "validator", "risk_officer", "provisioner",
                        "DONE"], {
        "triage": [[("fetch_work_item", {"work_item_id": work_item})]],
        "extractor": [[("recover_user_ids", {"work_item_id": work_item})]],
        "validator": [[("classify_user", {"userid": userid})]],
        "risk_officer": [[("request_human_approval",
                           {"reason": f"import {userid}", "userids": [userid]})]],
        "provisioner": [[("provision_jts_user", {"userid": userid})]],
    })


def approve_shown(cards: list):
    def decide(payload):
        cards.append(payload)
        shown = [i["userid"] for i in payload["items"]]
        return ApprovalDecision(thread_id=payload["thread_id"], approved=True,
                                approver="test:approver", plan_hash=payload["plan_hash"],
                                approved_userids=shown)
    return decide


async def _two_runs_at_once(settings):
    store = await get_store(settings)
    await MemoryStore(store).migrate()  # build_services does this once per process
    estate = SandboxEstate.default()
    try:
        async with checkpointer_for(settings) as checkpointer:
            shared = PerRunLLM()
            services = Services(settings=settings, store=store, client=None,
                                checkpointer=checkpointer, agent_llm=shared,
                                supervisor_llm=shared,
                                write_limit=asyncio.Semaphore(4))

            async def one(thread_id, work_item, userid, cards):
                _current.set(script(work_item, userid))
                graph, ctx = run_session(services, backend=SandboxBackend(estate))
                return await drive(graph, ctx, thread_id=thread_id,
                                   decide=approve_shown(cards), console=Console(),
                                   work_item_ids=[work_item])

            cards_a, cards_b = [], []
            report_a, report_b = await asyncio.gather(
                one("wi-1001", "1001", "AB12345", cards_a),
                one("wi-1002", "1002", "TB22322", cards_b))
            return report_a, report_b, cards_a, cards_b, estate
    finally:
        await store.close()


def test_two_concurrent_runs_keep_their_own_users_and_approvals(tmp_path):
    settings = local_settings(tmp_path, commit=True)
    report_a, report_b, cards_a, cards_b, estate = asyncio.run(_two_runs_at_once(settings))

    assert not report_a["halted"], report_a["halt_reason"]
    assert not report_b["halted"], report_b["halt_reason"]
    # Each approval card shows only users of that run's own work item.
    items_a = [i for c in cards_a for i in c["items"]]
    items_b = [i for c in cards_b for i in c["items"]]
    assert "AB12345" in {i["userid"] for i in items_a}
    assert "TB22322" in {i["userid"] for i in items_b}
    assert all(i["work_item_ids"] == ["1001"] for i in items_a), items_a
    assert all(i["work_item_ids"] == ["1002"] for i in items_b), items_b
    assert not {i["userid"] for i in items_a} & {i["userid"] for i in items_b}
    # Each run wrote only its own user, once.
    assert {(r["userid"], r["outcome"]) for r in report_a["results"]} == {("AB12345", "ok")}
    assert {(r["userid"], r["outcome"]) for r in report_b["results"]} == {("TB22322", "ok")}
    assert {"AB12345", "TB22322"} <= set(estate.people)


def test_each_session_gets_its_own_context_and_board_but_shares_the_connections(tmp_path):
    settings = local_settings(tmp_path, commit=True)
    limit = asyncio.Semaphore(4)
    services = Services(settings=settings, store=object(), client=object(),
                        checkpointer=None, agent_llm=PerRunLLM(),
                        supervisor_llm=PerRunLLM(), write_limit=limit)
    graph_a, ctx_a = run_session(services)
    graph_b, ctx_b = run_session(services)
    assert ctx_a is not ctx_b and graph_a is not graph_b
    assert ctx_a.store is ctx_b.store is services.store
    assert ctx_a.client is ctx_b.client is services.client
    # The write bound is the process's: EWM and JTS see every run's writes.
    assert ctx_a.limiter() is ctx_b.limiter() is limit
