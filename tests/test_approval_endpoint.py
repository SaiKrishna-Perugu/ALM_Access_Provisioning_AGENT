"""The approval endpoint: who may record a decision, and under what name.

The service sits behind the Identity-Aware Proxy, but its handlers must not
assume that: a request that reaches it any other way can carry any header.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from alm_api import main  # noqa: E402
from alm_api.security import issue_approval_token  # noqa: E402

SECRET = "approval-signing-key-for-tests"  # pragma: allowlist secret
THREAD = "wi-1001"
PLAN = "plan-hash-1"


class FakeStore:
    def __init__(self):
        self.decisions = []
        self.resumes = []

    async def get_approval(self, thread_id):
        if thread_id != THREAD:
            return None, None
        return SimpleNamespace(plan_hash=PLAN), None

    async def save_approval_decision(self, decision):
        self.decisions.append(decision)


@pytest.fixture
def api(monkeypatch):
    store = FakeStore()
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    monkeypatch.setattr(main.runtime, "services", SimpleNamespace(store=store))
    monkeypatch.setattr(main.runtime, "settings",
                        SimpleNamespace(approval_signing_secret_name="signing"))  # pragma: allowlist secret
    monkeypatch.setattr(main.runtime, "resolver",
                        SimpleNamespace(get=lambda _name: SECRET))

    async def queued(_store, thread_id, decision):
        store.resumes.append((thread_id, decision.approved))
        return 1

    from alm_agents import worker

    monkeypatch.setattr(worker, "submit_decision", queued)
    # No `with`: the app's startup (graph, database, reconcile loop) never runs.
    return TestClient(main.app), store


def test_a_forged_x_forwarded_user_cannot_approve(api):
    client, store = api
    response = client.post(f"/approvals/{THREAD}", json={"approved": True},
                           headers={"x-forwarded-user": "mallory@example.com"})
    assert response.status_code == 401
    assert store.decisions == []


def test_a_forged_iap_header_cannot_approve_once_an_audience_is_set(api, monkeypatch):
    client, store = api
    monkeypatch.setenv("ALM_IAP_AUDIENCE", "/projects/1/global/backendServices/2")
    response = client.post(
        f"/approvals/{THREAD}", json={"approved": True},
        headers={"x-goog-authenticated-user-email": "accounts.google.com:mallory@example.com"})
    assert response.status_code == 401
    assert store.decisions == []


def test_a_signed_token_records_the_decision(api):
    client, store = api
    token = issue_approval_token(SECRET, thread_id=THREAD, plan_hash=PLAN,
                                 expires_at=time.time() + 600)
    response = client.post(f"/approvals/{THREAD}",
                           json={"approved": True, "token": token})
    assert response.status_code == 200
    assert [d.approved for d in store.decisions] == [True]
    assert store.resumes == [(THREAD, True)]  # the resume is queued, not run here


def test_a_token_for_another_plan_is_refused(api):
    client, store = api
    token = issue_approval_token(SECRET, thread_id=THREAD, plan_hash="an-older-plan",
                                 expires_at=time.time() + 600)
    response = client.post(f"/approvals/{THREAD}",
                           json={"approved": True, "token": token})
    assert response.status_code == 403
    assert store.decisions == []


def test_an_unknown_thread_is_404(api):
    client, _store = api
    assert client.post("/approvals/wi-9999", json={"approved": True}).status_code == 404
