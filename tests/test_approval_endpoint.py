"""Approving a card: signed-in approvers, the two-person rule, one vote each.

The card itself cannot approve anything; decisions are made here, by people
the service knows (IAP or OIDC) holding the approver role.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from alm_api import main  # noqa: E402
from alm_core.models import (  # noqa: E402
    ApprovalItem,
    ApprovalRequest,
    RiskLevel,
    UserState,
    utcnow,
)
from alm_core.store.memory import MemoryStore  # noqa: E402

THREAD = "wi-1001"


def who(email):
    return {"x-goog-authenticated-user-email": f"accounts.google.com:{email}"}


ALICE, BOB, CAROL, OPS, VIEW = (who("alice@example.com"), who("bob@example.com"),
                                who("carol@example.com"), who("ops@example.com"),
                                who("view@example.com"))


def card(environment="TEST", high=False):
    return ApprovalRequest(
        run_id="r1", thread_id=THREAD, environment=environment,
        expires_at=utcnow() + timedelta(hours=1), plan_hash="plan-1",
        items=[ApprovalItem(userid="AB12345", work_item_ids=["1001"], action="import",
                            state=UserState.READY,
                            risk=RiskLevel.HIGH if high else RiskLevel.LOW),
               ApprovalItem(userid="CD67890", work_item_ids=["1001"], action="reactivate",
                            state=UserState.ARCHIVED, risk=RiskLevel.LOW)])


@pytest.fixture
def api(monkeypatch):
    store = MemoryStore()
    resumed = []
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    monkeypatch.setattr(main.runtime, "services", SimpleNamespace(store=store))
    monkeypatch.setattr(main.runtime, "settings", SimpleNamespace(
        auth_mode="iap", environment="TEST", shadow_mode=False,
        approvers_required=1, approvers_required_prod=2, approvers_required_high_risk=2,
        role_map=('{"alice@example.com": "approver", "bob@example.com": "approver", '
                  '"carol@example.com": ["approver", "operator"], '
                  '"ops@example.com": "operator", "view@example.com": "viewer"}')))

    async def queued(_store, thread_id, decision):
        resumed.append(decision)
        return 1

    from alm_agents import worker

    monkeypatch.setattr(worker, "submit_decision", queued)

    def park(request, requested_by="ops@example.com"):
        asyncio.run(store.save_approval_request(request))
        asyncio.run(store.upsert_run(THREAD, status="awaiting_approval", run_id="r1",
                                     requested_by=requested_by))

    return TestClient(main.app), store, resumed, park


def vote(client, headers, approved=True, userids=("AB12345", "CD67890")):
    return client.post(f"/approvals/{THREAD}", headers=headers,
                       json={"approved": approved, "approved_userids": list(userids)})


def test_only_signed_in_approvers_may_decide(api):
    client, _store, resumed, park = api
    park(card())
    assert vote(client, {}).status_code == 401
    assert vote(client, {"x-forwarded-user": "mallory@example.com"}).status_code == 401
    assert vote(client, OPS).status_code == 403          # operator, not approver
    assert client.get(f"/approvals/{THREAD}", headers=VIEW).status_code == 200
    assert resumed == []


def test_one_approver_suffices_on_test_and_the_run_resumes(api):
    client, store, resumed, park = api
    park(card())
    answer = vote(client, ALICE, userids=["AB12345"])
    assert answer.status_code == 200 and answer.json()["tally"]["complete"]
    assert resumed[0].approved and resumed[0].approved_userids == ["AB12345"]
    assert resumed[0].approver == "alice@example.com"
    audit = [e for e in store._audit if e.step == "approval_vote"]
    assert audit[0].approver == "alice@example.com"
    assert vote(client, BOB).status_code == 409           # already decided


def test_production_needs_two_distinct_approvers_and_writes_what_both_ticked(api):
    client, _store, resumed, park = api
    park(card(environment="PROD"))
    first = vote(client, ALICE, userids=["AB12345", "CD67890"])
    assert first.json()["tally"] == {"needed": 2, "approvals": ["alice@example.com"],
                                     "rejected_by": "", "complete": False}
    assert resumed == []
    assert vote(client, ALICE).status_code == 403          # one vote each
    vote(client, BOB, userids=["CD67890"])
    decision = resumed[0]
    assert decision.approved and decision.approved_userids == ["CD67890"]
    assert decision.approver == "alice@example.com + bob@example.com"


def test_a_high_risk_user_needs_two_and_the_starter_is_not_one(api):
    client, _store, resumed, park = api
    park(card(high=True), requested_by="carol@example.com")
    refused = vote(client, CAROL)
    assert refused.status_code == 403 and "started the run" in refused.json()["detail"]
    vote(client, ALICE)
    vote(client, BOB)
    assert resumed[0].approved


def test_one_rejection_rejects_for_everyone(api):
    client, _store, resumed, park = api
    park(card(environment="PROD"))
    vote(client, ALICE)
    vote(client, BOB, approved=False, userids=[])
    assert len(resumed) == 1 and not resumed[0].approved
    assert resumed[0].approver == "bob@example.com"


def test_approving_nobody_is_refused_and_unknown_users_are_ignored(api):
    client, _store, resumed, park = api
    park(card())
    assert vote(client, ALICE, userids=["ZZ99999"]).status_code == 422
    vote(client, ALICE, userids=["ZZ99999", "AB12345"])
    assert resumed[0].approved_userids == ["AB12345"]


def test_a_run_that_is_not_waiting_cannot_be_decided(api):
    client, store, _resumed, park = api
    park(card())
    asyncio.run(store.upsert_run(THREAD, status="running"))
    assert vote(client, ALICE).status_code == 409
    assert client.post("/approvals/wi-9999", headers=ALICE,
                       json={"approved": True}).status_code == 404
