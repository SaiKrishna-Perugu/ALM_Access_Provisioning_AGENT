"""The agentic system end to end, with a scripted model and the simulated estate.

A real Gemini model is not deterministic and not free, so these tests script the
model's decisions instead. Everything else is the production code: supervisor
loop, agents, policy engine, LangGraph interrupt and resume, guarded_write, the
idempotency ledger and the evidence gate. The scripts deliberately include the
mistakes a real model makes - writing before approval, inventing a user ID,
posting the same comment twice - to prove the guards catch them.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage  # noqa: E402

from alm_agents import llm as llm_module  # noqa: E402
from alm_agents.agent import portable_schema  # noqa: E402
from alm_agents.sandbox import Console, SandboxEstate, run_sandbox  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.logging import scrub_secrets  # noqa: E402
from alm_core.models import ApprovalDecision  # noqa: E402

AGENT_RE = re.compile(r"You are the (\w+) agent\.")
FAKE_KEY = "AIza" + "0" * 35


class ScriptedLLM:
    """Stands in for Gemini: the supervisor follows a plan, agents follow scripts.

    Each agent script is a list of turns; each turn is the tool calls the model
    makes in one response. An agent with no turns left calls finish.
    """

    def __init__(self, plan: list[str], scripts: dict[str, list[list[tuple[str, dict]]]]):
        self.plan = list(plan)
        self.scripts = {k: [list(t) for t in v] for k, v in scripts.items()}
        self.supervisor_calls = 0
        self._ids = 0

    def bind_tools(self, _tools, **_kw):
        return self

    async def ainvoke(self, messages, **_kw):
        found = AGENT_RE.search(str(messages[0].content))
        if found is None:
            self.supervisor_calls += 1
            chosen = self.plan.pop(0) if self.plan else "DONE"
            return AIMessage(content=json.dumps(
                {"next": chosen, "task": f"Scripted task for {chosen}.", "why": "scripted"}))
        turns = self.scripts.get(found.group(1)) or []
        turn = turns.pop(0) if turns else [("finish", {"summary": "done"})]
        calls = []
        for name, args in turn:
            self._ids += 1
            calls.append({"name": name, "args": args, "id": f"call-{self._ids}",
                          "type": "tool_call"})
        return AIMessage(content="", tool_calls=calls)


def sandbox_settings(**extra) -> Settings:
    base = Settings(_env_file=None, environment="TEST", orchestration="agentic",
                    llm_enabled=True, postgres_dsn="", shadow_mode=True, **extra)
    return base.model_copy(update={"shadow_mode": False})


def approve(payload: dict) -> ApprovalDecision:
    approve.calls.append(payload)
    return ApprovalDecision(thread_id=payload["thread_id"], approved=True,
                            approver="test:approver", plan_hash=payload["plan_hash"])


approve.calls = []

FULL_PLAN = ["triage", "validator", "provisioner", "risk_officer",
             "provisioner", "verifier", "evidence_officer", "closer", "DONE"]

APPROVED = ["AB12345", "CD67890", "TB22322"]
COMMENT = "AB12345 Alice Smith: imported into JTS; JazzUsers requested."

FULL_SCRIPTS = {
    "triage": [[("fetch_open_requests", {"limit": 10})]],
    "validator": [[("classify_user", {"userid": u}) for u in
                   ("AB12345", "CD67890", "EF11111", "GH22222", "TB22322")]
                  # A hallucinated ID: the policy must reject it before any lookup.
                  + [("classify_user", {"userid": "ALICE"})]],
    "provisioner": [
        # Before approval: a model trying to write early. Must be denied.
        [("provision_jts_user", {"userid": "AB12345"})],
        [("finish", {"summary": "waiting for approval"})],
        # After approval.
        [("provision_jts_user", {"userid": "AB12345"}),
         ("reactivate_jts_user", {"userid": "CD67890"}),
         ("provision_jts_user", {"userid": "TB22322"})],
        [("request_ad_group_membership", {"userid": u}) for u in APPROVED],
    ],
    "risk_officer": [[("request_human_approval",
                       {"reason": "three accounts to create or reactivate",
                        "userids": APPROVED})]],
    "verifier": [[("check_jazz_permission", {"userid": u}) for u in APPROVED]],
    "evidence_officer": [
        [("capture_evidence", {"userids": APPROVED})],
        [("attach_workitem_evidence", {"work_item_id": "1001", "userid": "AB12345"})],
    ],
    "closer": [
        [("post_workitem_comment", {"work_item_id": "1001", "text": COMMENT,
                                    "userid": "AB12345"})],
        # The same comment again: the ledger must turn it into a replay.
        [("post_workitem_comment", {"work_item_id": "1001", "text": COMMENT,
                                    "userid": "AB12345"})],
    ],
}


@pytest.fixture
def full_run(tmp_path):
    approve.calls = []
    fake = ScriptedLLM(FULL_PLAN, FULL_SCRIPTS)
    estate = SandboxEstate.default()
    report = asyncio.run(run_sandbox(
        sandbox_settings(), llm=fake, supervisor_llm=fake, estate=estate,
        console=Console(), decide=approve, shots_dir=str(tmp_path / "shots")))
    return report, estate


def _results(report, operation, userid):
    return [r for r in report["results"]
            if r["operation"] == operation and r["userid"] == userid]


def test_the_run_completes_and_provisions_exactly_the_approved_users(full_run):
    report, estate = full_run
    assert not report["halted"], report["halt_reason"]
    assert report["approval_rounds"] == 1
    assert sorted(report["estate"]["active_with_role"]) == [
        "AB12345", "CD67890", "EF11111", "TB22322"]
    assert "GH22222" not in estate.ad_requests
    assert sorted(estate.ad_requests) == APPROVED


def test_archived_user_is_reactivated_not_recreated(full_run):
    report, _ = full_run
    assert [r["outcome"] for r in _results(report, "jts_unarchive", "CD67890")] == ["ok"]
    assert not _results(report, "jts_create", "CD67890")


def test_results_are_not_duplicated_across_hops(full_run):
    """results is an append-only channel; each write must appear once."""
    report, _ = full_run
    assert len(_results(report, "jts_create", "AB12345")) == 1
    assert len(_results(report, "ad_group_add", "TB22322")) == 1


def test_the_approval_card_is_sent_once_despite_the_resume(full_run):
    """The interrupted node re-runs on resume; it must not notify again."""
    report, _ = full_run
    assert report["notifications_sent"] == 1
    assert len(approve.calls) == 1


def test_an_early_write_is_denied_by_policy_before_approval(full_run):
    report, _ = full_run
    reasons = report["policy"]["denial_reasons"]
    assert any("no human approval" in r for r in reasons), reasons


def test_an_invented_user_id_is_rejected_by_policy(full_run):
    report, estate = full_run
    assert any("'ALICE' is not a valid user ID" in r
               for r in report["policy"]["denial_reasons"])
    assert "ALICE" not in estate.people


def test_a_model_recovered_user_is_flagged_high_risk_for_the_approver(full_run):
    item = next(i for i in approve.calls[0]["items"] if i["userid"] == "TB22322")
    assert item["risk"] == "high"
    assert any("recovered by the LLM" in r for r in item["risk_reasons"])


def test_a_repeated_comment_is_a_ledger_replay_not_a_second_post(full_run):
    report, estate = full_run
    comments = _results(report, "workitem_comment", "AB12345")
    assert [c["replayed"] for c in comments] == [False, True]
    assert len(estate.comments["1001"]) == 1


def test_evidence_passes_the_real_validation_gate_and_is_attached(full_run):
    report, estate = full_run
    assert estate.attachments["1001"] == ["AB12345.png"]
    assert [r["outcome"] for r in _results(report, "workitem_attach", "AB12345")] == ["ok"]


def test_every_write_is_audited_with_the_approver(full_run):
    report, _ = full_run
    writes = [e for e in report["audit_events"] if e["step"] == "jts_provision"
              and e["outcome"] == "ok"]
    assert writes and all(e["approver"] == "test:approver" for e in writes)


def test_shadow_mode_writes_nothing_even_after_approval(tmp_path):
    approve.calls = []
    fake = ScriptedLLM(FULL_PLAN, FULL_SCRIPTS)
    estate = SandboxEstate.default()
    settings = sandbox_settings().model_copy(update={"shadow_mode": True})
    report = asyncio.run(run_sandbox(settings, llm=fake, estate=estate, console=Console(),
                                     decide=approve, shots_dir=str(tmp_path)))
    assert report["policy"]["writes"] == 0
    assert estate.ad_requests == []
    assert estate.comments["1001"] == []
    assert sorted(report["estate"]["active_with_role"]) == ["EF11111"]


def test_a_rejected_batch_halts_without_writing(tmp_path):
    fake = ScriptedLLM(FULL_PLAN, FULL_SCRIPTS)
    estate = SandboxEstate.default()

    def reject(payload):
        return ApprovalDecision(thread_id=payload["thread_id"], approved=False,
                                approver="test:approver", plan_hash=payload["plan_hash"])

    report = asyncio.run(run_sandbox(sandbox_settings(), llm=fake, estate=estate,
                                     console=Console(), decide=reject,
                                     shots_dir=str(tmp_path)))
    assert report["halted"] and "rejected" in report["halt_reason"]
    assert report["policy"]["writes"] == 0
    assert estate.ad_requests == []


# ------------------------------------------------------------------ supervisor

def test_supervisor_reads_gemini_list_content():
    """Gemini may return content as parts; the supervisor must still parse it."""
    from alm_agents.supervisor import decide

    class PartsLLM:
        async def ainvoke(self, _messages):
            return AIMessage(content=[
                {"type": "text", "text": '{"next": "validator", "task": "classify AB12345",'},
                {"type": "text", "text": ' "why": "users are not validated"}'}])

    decision = asyncio.run(decide(PartsLLM(), history=[], board_snapshot={},
                                  policy_summary={}))
    assert decision.next_agent == "validator" and not decision.fallback


def test_supervisor_falls_back_when_the_model_talks_instead_of_answering():
    from alm_agents.supervisor import decide

    class ChattyLLM:
        async def ainvoke(self, _messages):
            return AIMessage(content="I think the validator should probably go next.")

    decision = asyncio.run(decide(ChattyLLM(), history=[], board_snapshot={},
                                  policy_summary={}))
    assert decision.fallback


# --------------------------------------------------------------------- schema

def test_tool_schemas_are_portable_to_gemini():
    from alm_agents.memory import MemoryStore
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.tools.base import ToolContext

    ctx = ToolContext(settings=sandbox_settings(), client=None, store=None, run_id="t")
    registry = build_registry(ctx, Blackboard(), MemoryStore(None))
    schemas = registry.as_openai_schema(registry.names())
    text = json.dumps(schemas)
    for banned in ('"$ref"', '"$defs"', '"title"', '"anyOf"', '"default"'):
        assert banned not in text, banned

    genai = pytest.importorskip("langchain_google_genai._function_utils")
    declarations = genai.convert_to_genai_function_declarations(schemas)
    names = {fd.name for tool in declarations for fd in tool.function_declarations}
    assert names == set(registry.names())


def test_portable_schema_collapses_optional_fields():
    from pydantic import BaseModel

    class Args(BaseModel):
        userid: str | None = None

    schema = portable_schema(Args)
    assert schema["properties"]["userid"] == {"type": "string", "nullable": True}


# ------------------------------------------------------------------- provider

@pytest.fixture
def no_key(monkeypatch):
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALM_SECRET_DIR", "/nonexistent-alm-secrets")
    llm_module.reset_clients()
    yield
    llm_module.reset_clients()


def test_gemini_api_provider_builds_a_rate_limited_client(no_key, monkeypatch):
    pytest.importorskip("langchain_google_genai")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    settings = sandbox_settings(agent_model="gemini-3.5-flash")
    client = llm_module.get_agent_llm(settings)
    assert type(client).__name__ == "ChatGoogleGenerativeAI"
    assert client.rate_limiter is not None
    assert client.reasoning_effort == "low"
    # One limiter for the whole process: the quota belongs to the key.
    assert llm_module.get_supervisor_llm(settings).rate_limiter is client.rate_limiter


def test_gemini_api_provider_without_a_key_degrades_to_no_client(no_key):
    pytest.importorskip("langchain_google_genai")
    assert llm_module.get_agent_llm(sandbox_settings()) is None


def test_claude_is_refused_on_the_gemini_api_provider():
    with pytest.raises(Exception, match="Vertex AI Model Garden"):
        Settings(_env_file=None, llm_provider="gemini_api", agent_model="claude-sonnet-5")


def test_vertex_provider_still_requires_a_project(no_key):
    with pytest.raises(Exception, match="GOOGLE_CLOUD_PROJECT"):
        Settings(_env_file=None, llm_provider="vertex", orchestration="agentic")


def test_thinking_level_maps_per_model_generation():
    thinking = llm_module._thinking_kwargs
    assert thinking("gemini-3.5-flash", "low", ["minimal", "low"]) == {"thinking_level": "low"}
    # Pro has no "minimal"; the lightest supported level is used instead.
    assert thinking("gemini-3.1-pro-preview", "minimal", ["low", "high"]) == \
        {"thinking_level": "low"}
    assert thinking("gemini-2.5-flash", "minimal", None) == {"thinking_budget": 0}
    assert thinking("gemini-2.5-pro", "minimal", None) == {}
    assert thinking("gemini-3.5-flash", "default", None) == {}


def test_a_google_api_key_never_survives_redaction():
    from alm_core.logging import redact_mapping

    assert FAKE_KEY not in scrub_secrets(f"GET /v1beta/models?key={FAKE_KEY} 403")
    assert redact_mapping({"error": f"bad {FAKE_KEY}"})["error"] == "bad [redacted]"
    assert redact_mapping({"x-goog-api-key": "anything"})["x-goog-api-key"] == "[redacted]"


# ------------------------------------------------------------------- approval

def test_open_request_reuses_the_pending_request_for_the_same_plan():
    from datetime import timedelta

    from alm_agents.nodes.approval import open_request
    from alm_core.models import ApprovalRequest, utcnow
    from alm_core.store.memory import MemoryStore

    async def scenario():
        store = MemoryStore()
        first = ApprovalRequest(thread_id="t", run_id="r", environment="TEST",
                                expires_at=utcnow() + timedelta(hours=1), plan_hash="A")
        again = first.model_copy(update={"expires_at": utcnow() + timedelta(hours=9)})
        kept, is_new = await open_request(store, first)
        assert is_new
        kept, is_new = await open_request(store, again)
        assert not is_new and kept.expires_at == first.expires_at  # expiry not reset

        await store.save_approval_decision(ApprovalDecision(
            thread_id="t", approved=True, approver="a", plan_hash="A"))
        second_round = first.model_copy(update={"plan_hash": "B"})
        kept, is_new = await open_request(store, second_round)
        assert is_new
        _request, decision = await store.get_approval("t")
        assert decision is None  # a decision on plan A does not decide plan B

    asyncio.run(scenario())


def test_checkpoint_serde_round_trips_state_types_under_the_allowlist():
    """Unlisted types will be refused by a future LangGraph; ours are listed."""
    from alm_agents.graph import checkpoint_serde
    from alm_core.models import RiskLevel, UserState, UserStatus

    serde = checkpoint_serde()
    status = UserStatus(userid="AB12345", state=UserState.READY, risk=RiskLevel.HIGH)
    decision = ApprovalDecision(thread_id="t", approved=True, approver="a", plan_hash="h")
    restored = serde.loads_typed(serde.dumps_typed({"s": status, "d": decision}))
    assert restored["s"] == status and restored["d"] == decision


# ------------------------------------------------- which kind of Google key

class FakeGoogle:
    """Canned answers from the Gemini API and Vertex AI endpoints."""

    def __init__(self, gemini: tuple[int, str], vertex_status: int):
        self.gemini, self.vertex_status = gemini, vertex_status

    @staticmethod
    def _response(status, reason=""):
        class R:
            status_code = status
            text = ""

            def json(self):
                if status == 200:
                    return {"models": []}
                return {"error": {"message": f"refused ({reason})",
                                  "details": [{"reason": reason}]}}
        return R()

    def get(self, *_a, **_k):
        return self._response(*self.gemini)

    def post(self, *_a, **_k):
        return self._response(self.vertex_status, "CREDENTIALS_MISSING")


class Lines(Console):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def line(self, text=""):
        self.lines.append(text)


@pytest.mark.parametrize(("gemini", "vertex", "expected"), [
    ((400, "API_KEY_INVALID"), 200, "ALM_LLM_PROVIDER=vertex_express"),
    ((400, "API_KEY_INVALID"), 401, "copied incompletely"),
    ((403, "SERVICE_DISABLED"), 401, "Generative Language API switched off"),
    ((403, "API_KEY_SERVICE_BLOCKED"), 401, "API restrictions"),
    # A key restricted to Vertex AI is blocked on the Gemini API, not unknown.
    ((403, "API_KEY_SERVICE_BLOCKED"), 200, "ALM_LLM_PROVIDER=vertex_express"),
    ((403, "SERVICE_DISABLED"), 200, "ALM_LLM_PROVIDER=vertex_express"),
])
def test_check_explains_what_is_wrong_with_the_key(monkeypatch, gemini, vertex, expected):
    import requests

    from alm_agents.sandbox import _list_models

    fake = FakeGoogle(gemini, vertex)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    console = Lines()
    assert _list_models(FAKE_KEY, console, probe_model="gemini-3.5-flash") is None
    assert any(expected in line for line in console.lines), console.lines
    assert not any(FAKE_KEY in line for line in console.lines)


def test_a_vertex_express_key_is_checked_against_vertex(monkeypatch):
    import requests

    from alm_agents.sandbox import _vertex_express_available

    monkeypatch.setattr(requests, "post", FakeGoogle((200, ""), 200).post)
    console = Lines()
    assert _vertex_express_available(FAKE_KEY, "gemini-3.5-flash", console)
    monkeypatch.setattr(requests, "post", FakeGoogle((200, ""), 401).post)
    assert not _vertex_express_available(FAKE_KEY, "gemini-3.5-flash", console)
    assert any("ALM_LLM_PROVIDER=gemini_api" in line for line in console.lines)


def test_vertex_express_builds_a_keyed_vertex_client_without_a_project(no_key, monkeypatch):
    pytest.importorskip("langchain_google_genai")
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    client = llm_module.get_agent_llm(sandbox_settings(llm_provider="vertex_express"))
    assert client.vertexai and not client.project
    with pytest.raises(Exception, match="Vertex AI Model Garden"):
        Settings(_env_file=None, llm_provider="vertex_express",
                 agent_model="claude-sonnet-5")
