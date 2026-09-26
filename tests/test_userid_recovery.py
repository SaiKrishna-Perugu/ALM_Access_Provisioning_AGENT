"""User-ID recovery: code finds the candidates, a model only judges them.

The property that matters is structural: whatever the judge says, the IDs that
come out are tokens copied from the text. These tests use a fake TypeSafe client
built on the real SDK question types, so no key and no network are needed.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

pytest.importorskip("pydantic_settings")

from alm_agents import llm  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.models import RequestedUser, RiskLevel, UserState  # noqa: E402
from alm_core.tools.jts import _risk  # noqa: E402

ROW = "please also add Tom Baker (TB22322), thanks - form ISO9001 attached"


class FakeTypeSafe:
    """Answers each Noul from a table keyed by the user ID named in it."""

    def __init__(self, probabilities: dict[str, float], fail: bool = False):
        self.probabilities = probabilities
        self.fail = fail
        self.requests: list[dict] = []

    def system_one(self, *, state, questions, **_kw):
        self.requests.append({"state": state, "questions": questions})
        if self.fail:
            raise ConnectionError("typesafe unreachable")

        class Answer:
            def __init__(self, value):
                self.noul = value

        class Response:
            nouls = {}

        response = Response()
        response.nouls = {}
        for qid, question in questions.items():
            userid = re.search(r"user ID (\S+)", question.instructions).group(1)
            response.nouls[qid] = Answer(self.probabilities.get(userid, 0.0))
        return response


def settings(**extra) -> Settings:
    return Settings(_env_file=None, orchestration="deterministic", llm_enabled=True,
                    **extra)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    llm.reset_clients()
    # Never reach a real model from these tests.
    monkeypatch.setattr(llm, "_invoke", lambda *_a, **_k: "")
    yield
    llm.reset_clients()


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(llm, "_typesafe_key", lambda _s: "test-key")


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.setattr(llm, "_typesafe_key", lambda _s: None)


# ------------------------------------------------------------------ candidates

def test_candidates_are_every_userid_shaped_token_upper_cased_and_deduplicated():
    assert llm.userid_candidates("add tb22322; TB22322 again; Q3 plan; SF58083.") == [
        "TB22322", "SF58083"]


def test_a_row_without_candidates_never_reaches_a_model(with_key, monkeypatch):
    called = []
    monkeypatch.setattr(llm, "_invoke", lambda *a, **k: called.append(a) or "")
    fake = FakeTypeSafe({})
    assert llm.extract_users(settings(), "please add Tom Baker, thanks", "1002",
                             typesafe_client=fake) == []
    assert fake.requests == [] and called == []


# -------------------------------------------------------------------- typesafe

def test_typesafe_selects_the_requested_user_and_rejects_the_reference(with_key):
    pytest.importorskip("typesafe_sdk")
    fake = FakeTypeSafe({"TB22322": 0.93, "ISO9001": 0.04})
    users = llm.extract_users(settings(), ROW, "1002", "validation team",
                              typesafe_client=fake)

    assert [(u.userid, u.extraction_confidence, u.extracted_by_llm) for u in users] == [
        ("TB22322", 0.93, True)]
    assert users[0].work_item_ids == ["1002"]
    request = fake.requests[0]
    assert request["state"]["candidates"] == {"c0": "TB22322", "c1": "ISO9001"}
    assert all(q.criteria is not None for q in request["questions"].values())


def test_the_threshold_decides_what_is_proposed(with_key):
    pytest.importorskip("typesafe_sdk")
    fake = FakeTypeSafe({"TB22322": 0.62})
    recovery = llm.recover_userids(settings(extraction_min_probability=0.7), ROW,
                                   "1002", typesafe_client=fake)
    assert recovery.accepted == {} and recovery.rejected["TB22322"] == 0.62


def test_email_addresses_never_reach_the_judge(with_key):
    pytest.importorskip("typesafe_sdk")
    fake = FakeTypeSafe({"TB22322": 0.9})
    llm.extract_users(settings(), "Tom Baker tom.baker@example.com TB22322", "1002",
                      typesafe_client=fake)
    assert "example.com" not in json.dumps(fake.requests[0]["state"])


def test_auto_falls_back_to_gemini_when_typesafe_fails(with_key, monkeypatch):
    pytest.importorskip("typesafe_sdk")
    monkeypatch.setattr(llm, "_invoke", lambda *_a, **_k: json.dumps(
        {"users": [{"userid": "TB22322", "confidence": 0.8}]}))
    recovery = llm.recover_userids(settings(), ROW, "1002",
                                   typesafe_client=FakeTypeSafe({}, fail=True))
    assert recovery.method == "gemini" and "unreachable" in recovery.error
    assert recovery.accepted == {"TB22322": 0.8}


def test_explicit_typesafe_does_not_fall_back(with_key, monkeypatch):
    pytest.importorskip("typesafe_sdk")
    monkeypatch.setattr(llm, "_invoke", lambda *_a, **_k: pytest.fail("fell back"))
    recovery = llm.recover_userids(settings(extraction_provider="typesafe"), ROW, "1002",
                                   typesafe_client=FakeTypeSafe({}, fail=True))
    assert recovery.accepted == {} and recovery.error


# ---------------------------------------------------------------------- gemini

def test_gemini_cannot_introduce_an_id_that_is_not_in_the_text(without_key, monkeypatch):
    """The gap this change closed: a well-formed invented ID used to pass."""
    monkeypatch.setattr(llm, "_invoke", lambda *_a, **_k: json.dumps({"users": [
        {"userid": "KX40912", "confidence": 0.99},   # well-formed, invented
        {"userid": "tb22322", "confidence": 0.9}]}))
    users = llm.extract_users(settings(), ROW, "1002")
    assert [u.userid for u in users] == ["TB22322"]


def test_without_a_typesafe_key_auto_uses_gemini(without_key, monkeypatch):
    monkeypatch.setattr(llm, "_invoke", lambda *_a, **_k: json.dumps(
        {"users": [{"userid": "TB22322", "confidence": 0.9}]}))
    assert llm.recover_userids(settings(), ROW, "1002").method == "gemini"


# ------------------------------------------------------------------ downstream

def test_the_approver_sees_the_probability():
    user = RequestedUser(userid="TB22322", extracted_by_llm=True,
                         extraction_confidence=0.62)
    risk, reasons = _risk(user, UserState.READY, "tom.baker@example.com")
    assert risk == RiskLevel.HIGH
    assert any("(probability 0.62)" in r for r in reasons)


def test_the_agent_tool_adds_only_selected_users_with_their_source(with_key, monkeypatch):
    pytest.importorskip("langgraph")
    from alm_agents.memory import MemoryStore
    from alm_agents.sandbox import SandboxBackend, SandboxEstate
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.store.memory import MemoryStore as LedgerStore
    from alm_core.tools.base import ToolContext

    monkeypatch.setattr(llm, "_typesafe_judge",
                        lambda _s, _state, candidates, _c=None:
                        {c: (0.91 if c == "TB22322" else 0.02) for c in candidates})
    ctx = ToolContext(settings=settings(), client=None, store=LedgerStore(), run_id="t")
    board = Blackboard()
    registry = build_registry(ctx, board, MemoryStore(None),
                              backend=SandboxBackend(SandboxEstate.default()))

    async def scenario():
        await registry.get("fetch_work_item").run(work_item_id="1002")
        return json.loads(await registry.get("recover_user_ids").run(work_item_id="1002"))

    result = asyncio.run(scenario())
    assert result["judged_by"] == "typesafe"
    assert result["added_to_run"] == ["TB22322"]
    recovered = board.users["TB22322"]
    assert recovered.extracted_by_llm and recovered.extraction_confidence == 0.91
    assert recovered.work_item_ids == ["1002"]
    # Users the parser read correctly are untouched.
    assert not board.users["EF11111"].extracted_by_llm


def test_the_real_sdk_request_and_response_round_trip(with_key):
    """The fake above skips the SDK's encoding; this drives the real client."""
    pytest.importorskip("typesafe_sdk")
    httpx2 = pytest.importorskip("httpx2")
    from typesafe_sdk import TypeSafeClient

    sent = {}

    def handler(request):
        body = json.loads(request.content)
        sent.update(body)
        return httpx2.Response(200, json={
            "model": "jev-1.13.0", "usage": {"input_tokens": 300, "output_tokens": 4},
            "answers": {q: {"type": "noul",
                            "noul": 0.93 if "TB22322" in v["instructions"] else 0.03}
                        for q, v in body["questions"].items()}})

    client = TypeSafeClient(api_key="test", model="jev-latest",  # pragma: allowlist secret
                            transport=httpx2.MockTransport(handler))
    users = llm.extract_users(settings(), ROW, "1002", typesafe_client=client)
    assert [u.userid for u in users] == ["TB22322"]
    assert sent["model"] == "jev-latest"
    assert sent["questions"]["c0"]["type"] == "noul"
    assert set(sent["questions"]["c0"]["criteria"]) == {"true", "false"}
