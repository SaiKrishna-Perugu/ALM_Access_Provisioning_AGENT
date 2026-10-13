"""Dry-run regressions: compatible OSLC lookup and bounded extraction work."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from test_agentic_sandbox import ScriptedLLM, sandbox_settings  # noqa: E402

from alm_agents import llm, supervisor  # noqa: E402
from alm_agents.memory import MemoryStore  # noqa: E402
from alm_agents.sandbox import Console, SandboxBackend, SandboxEstate, run_sandbox  # noqa: E402
from alm_agents.toolkit import Blackboard, build_registry  # noqa: E402
from alm_core import oslc  # noqa: E402
from alm_core.models import RequestedUser, WorkItem  # noqa: E402
from alm_core.store.memory import MemoryStore as LedgerStore  # noqa: E402
from alm_core.tools import ewm  # noqa: E402
from alm_core.tools.base import ToolContext  # noqa: E402


@pytest.fixture
def lookup(monkeypatch):
    row = {
        "dcterms:identifier": "1001", "dcterms:title": "Access request",
        "dcterms:type": ewm.TYPE_ID,
        "rtc_cm:state": {"rdf:resource": "https://example.intra/state.open"},
        oslc.F_ACCESS_TYPE: {"rdf:resource": "https://example.intra/access.new"},
        oslc.F_DOMAIN: {"rdf:resource": "https://example.intra/domain.EMEA"},
        oslc.F_WORKAREAS: {"rdf:resource": "https://example.intra/area.engineering"},
        oslc.F_JUSTIFICATION: "<p>Needs access</p>",
        oslc.F_NEW_USERS: "Smith,Alice,alice@example.com,AB12345",
    }
    queries = []

    def collect(_session, _url, params, **_kw):
        queries.append(params)
        return [row]

    monkeypatch.setattr(ewm, "collect_results", collect)
    monkeypatch.setattr(ewm, "_project_uuid", lambda _ctx: "project")
    ctx = SimpleNamespace(
        settings=SimpleNamespace(ewm_server="https://example.intra/ccm", state_ids=[],
                                 timeout=(5, 30), verify=True),
        client=SimpleNamespace(session=lambda *_a, **_kw: object()))
    return ctx, row, queries


def test_lookup_uses_identifier_only_and_hydrates_like_the_queue(lookup):
    ctx, row, queries = lookup
    item = ewm._fetch_one(ctx, "1001")
    assert queries[0]["oslc.where"] == "dcterms:identifier=1001"
    assert item == ewm._fetch_queue(ctx, 1)[0]
    assert (item.access_type, item.domain, item.work_areas, item.justification) == (
        "new", "EMEA", "engineering", "Needs access")
    assert item.new_users_raw == row[oslc.F_NEW_USERS]
    assert [u.userid for u in item.users] == ["AB12345"]


@pytest.mark.parametrize("value", ["defect", "", None, ewm.TYPE_ID + "Impostor",
                                    {"rdf:resource": "https://example.intra/types/task"}])
def test_lookup_rejects_missing_or_different_types(lookup, value):
    ctx, row, _ = lookup
    row["dcterms:type"] = value
    assert ewm._fetch_one(ctx, "1001") is None


@pytest.mark.parametrize("field", ["dcterms:type", "rtc_cm:type"])
def test_lookup_accepts_type_resources_and_legacy_user_field(lookup, field):
    ctx, row, _ = lookup
    row.pop("dcterms:type")
    row[field] = {"rdf:resource": "https://example.intra/types/" + ewm.TYPE_ID}
    row[oslc.F_NEW_USERS_LEGACY] = row.pop(oslc.F_NEW_USERS)
    assert ewm._fetch_one(ctx, "1001").users[0].userid == "AB12345"


def exhausted_history(snapshot):
    return [{"agent": "triage", "stopped": "finish"},
            {"agent": "extractor", "stopped": "iteration limit", "denials": 0,
             "routing_scope": supervisor.routing_scope(snapshot, "extractor")}]


def test_guided_exhaustion_advances_without_a_supervisor_model_call():
    snapshot = {"work_items": ["1001"], "users": {"AB12345": "not validated"}}
    history = exhausted_history(snapshot)
    assert supervisor.guided_next(history, snapshot, shadow=True).next_agent == "validator"
    # Validation/approval alone must not send the extractor back over the same rows.
    snapshot.update(users={"AB12345": "ready"}, approved=True)
    assert "extractor" in supervisor.exhausted_agents(history, snapshot)
    snapshot["work_items"].append("1002")
    assert "extractor" not in supervisor.exhausted_agents(history, snapshot)
    assert supervisor.guided_next(history, snapshot, shadow=True).next_agent == "extractor"


def test_supervisor_cannot_choose_the_same_exhausted_agent():
    snapshot = {"work_items": ["1001"], "users": {"AB12345": "ready"}}
    fake = ScriptedLLM(["extractor"], {})
    decision = asyncio.run(supervisor.decide(
        fake, history=exhausted_history(snapshot), board_snapshot=snapshot, policy_summary={}))
    assert decision.next_agent == "validator" and decision.fallback


def test_new_approval_unblocks_an_exhausted_provisioner():
    snapshot = {"work_items": ["1001"], "users": ["AB12345"], "approved": False}
    history = [{"agent": "provisioner", "stopped": "iteration limit",
                "routing_scope": supervisor.routing_scope(snapshot, "provisioner")}]
    assert "provisioner" in supervisor.exhausted_agents(history, snapshot)
    snapshot["approved"] = True
    assert "provisioner" not in supervisor.exhausted_agents(history, snapshot)


@pytest.mark.parametrize("raw", ["Smith,Alice,alice@example.com,AB12345", ""])
def test_parsed_users_never_trigger_nested_recovery(raw, monkeypatch):
    monkeypatch.setattr(llm, "recover_userids", lambda *_a, **_kw: pytest.fail("nested model"))
    board = Blackboard()
    board.work_items["1001"] = WorkItem(
        work_item_id="1001", new_users_raw=raw, justification="Also mentions CD67890",
        users=[RequestedUser(userid="AB12345")])
    ctx = ToolContext(settings=sandbox_settings(), client=None, store=LedgerStore(), run_id="t")
    registry = build_registry(ctx, board, MemoryStore(None),
                              backend=SandboxBackend(SandboxEstate.default()))
    result = asyncio.run(registry.get("recover_user_ids").run(work_item_id="1001"))
    assert "recovery not needed" in result
    extraction = json.loads(board.snapshot())["extraction"]["1001"]
    assert extraction["parsed_users"] == ["AB12345"] and extraction["rejected_rows"] == 0


def test_guided_sandbox_does_not_repeat_an_exhausted_extractor(tmp_path):
    fake = ScriptedLLM([], {
        "triage": [[("fetch_open_requests", {})]],
        "extractor": [[("parse_new_users_field", {"work_item_id": "1001"})]] * 14,
    })
    events = []
    settings = sandbox_settings().model_copy(update={"shadow_mode": True,
                                                      "orchestration": "guided"})
    # Console events expose the real routing decisions across graph hops.
    class RecordingConsole(Console):
        def __call__(self, kind, data):
            events.append((kind, data))

    report = asyncio.run(run_sandbox(settings, llm=fake, supervisor_llm=fake,
                                    console=RecordingConsole(),
                                    shots_dir=str(tmp_path / "shots")))
    assert not report["halted"], report["halt_reason"]
    routes = [data["next"] for kind, data in events if kind == "supervisor"]
    assert routes.count("extractor") == 1 and "validator" in routes
    assert fake.supervisor_calls == 0


def test_repeated_recovery_reuses_judgement_until_the_source_changes(monkeypatch):
    calls = []

    def recover(_settings, text, _wid, _summary):
        calls.append(text)
        return llm.Recovery(method="scripted", candidates=["TB22322"],
                            accepted={"TB22322": 0.9})

    monkeypatch.setattr(llm, "recover_userids", recover)
    board = Blackboard()
    board.work_items["1001"] = WorkItem(work_item_id="1001",
                                        justification="Please add TB22322")
    ctx = ToolContext(settings=sandbox_settings(), client=None, store=LedgerStore(), run_id="t")
    registry = build_registry(ctx, board, MemoryStore(None),
                              backend=SandboxBackend(SandboxEstate.default()))

    async def scenario():
        tool = registry.get("recover_user_ids")
        first = json.loads(await tool.run(work_item_id="1001"))
        second = json.loads(await tool.run(work_item_id="1001"))
        assert first["added_to_run"] == ["TB22322"] and second["added_to_run"] == []
        assert len(calls) == 1
        board.work_items["1001"].justification += " for a new project"
        await tool.run(work_item_id="1001")
        assert len(calls) == 2

    asyncio.run(scenario())
