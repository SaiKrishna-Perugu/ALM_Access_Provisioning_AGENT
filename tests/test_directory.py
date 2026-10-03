"""AD group membership: Microsoft Graph, and GPT's "outcome unknown" rule.

Graph is driven against a fake HTTP session that answers like Graph does.
GPT is driven through the real ``GptSession.add_member`` over fake page steps.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("pydantic")

from alm_core.config import Settings  # noqa: E402
from alm_core.errors import ConfigError, OutcomeUnknown  # noqa: E402
from alm_core.models import ApprovalDecision, RequestedUser, SourceWorkItem  # noqa: E402
from alm_core.store.memory import MemoryStore  # noqa: E402
from alm_core.tools import directory  # noqa: E402
from alm_core.tools.base import ToolContext  # noqa: E402


def settings(**extra) -> Settings:
    base = {"_env_file": None, "environment": "TEST", "orchestration": "deterministic",
            "llm_enabled": False, "shadow_mode": False, "ledger_path": "unused.db",
            "ad_directory": "graph", "graph_tenant_id": "tenant-1",
            "graph_client_id": "client-1"}
    base.update(extra)
    return Settings(**base)


class Reply:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeGraph:
    """Answers the calls GraphDirectory makes, and records them."""

    def __init__(self, add_replies=None, users=None):
        self.calls = []
        self.add_replies = list(add_replies or [Reply(204)])
        self.users = {"AB12345": "user-ab"} if users is None else users
        self.verify = None

    def post(self, url, data=None, timeout=None):
        self.calls.append(("TOKEN", url, data))
        return Reply(200, {"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, headers=None, timeout=None, json=None):
        self.calls.append((method, url, headers, json))
        assert headers["Authorization"] == "Bearer tok"
        if "/users?" in url:
            sam = url.split("eq '")[1].split("'")[0]
            found = [{"id": self.users[sam]}] if sam in self.users else []
            return Reply(200, {"value": found})
        if "/groups?" in url:
            return Reply(200, {"value": [{"id": "group-1"}]})
        if url.endswith("/members/$ref"):
            return self.add_replies.pop(0)
        raise AssertionError(url)


class Secrets:
    def get(self, _name, refresh=False):
        return "client-secret"  # pragma: allowlist secret


def graph(fake, **extra):
    return directory.GraphDirectory(settings(**extra), resolver=Secrets(), http=fake)


def test_graph_adds_a_member_resolving_user_and_group(monkeypatch):
    fake = FakeGraph()
    ok, message = graph(fake).add_member(userid="AB12345", group="GR_D-JazzUser-NA")
    assert ok and "added" in message
    token_call = fake.calls[0]
    assert token_call[0] == "TOKEN" and "tenant-1" in token_call[1]
    assert token_call[2]["client_secret"] == "client-secret"  # pragma: allowlist secret
    add = fake.calls[-1]
    assert add[1].endswith("/groups/group-1/members/$ref")
    assert add[3] == {"@odata.id": f"{directory.GRAPH}/directoryObjects/user-ab"}


def test_already_a_member_is_success_and_an_unknown_user_is_not():
    already = Reply(400, {"error": {"message": "One or more added object references "
                                               "already exist for the following modified "
                                               "properties: 'members'."}})
    assert graph(FakeGraph([already])).add_member(userid="AB12345", group="G")[0]
    ok, message = graph(FakeGraph()).add_member(userid="ZZ99999", group="G")
    assert not ok and "not found" in message


def test_an_on_premises_group_is_named_as_the_reason():
    refused = Reply(400, {"error": {"message": "Unable to update the specified properties "
                                               "for on-premises mastered Directory Sync "
                                               "objects or objects currently undergoing "
                                               "migration."}})
    with pytest.raises(ConfigError, match="on-premises AD"):
        graph(FakeGraph([refused])).add_member(userid="AB12345", group="G")


def test_throttling_is_retried_with_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(directory.time, "sleep", slept.append)
    fake = FakeGraph([Reply(429, headers={"Retry-After": "3"}), Reply(204)])
    assert graph(fake).add_member(userid="AB12345", group="G")[0]
    assert slept == [3.0]


def test_a_configured_group_id_skips_the_lookup_and_upn_lookup_works():
    fake = FakeGraph()

    def by_upn(method, url, headers=None, timeout=None, json=None):
        fake.calls.append((method, url))
        if "/users/" in url:
            return Reply(200, {"id": "user-upn"})
        return Reply(204)

    fake.request = by_upn
    g = graph(fake, graph_group_id="fixed-group", graph_user_lookup="upn",
              graph_upn_suffix="example.com")
    assert g.add_member(userid="AB12345", group="ignored")[0]
    assert any("AB12345%40example.com" in c[1] for c in fake.calls if c[0] == "GET")
    assert not any("/groups?" in c[1] for c in fake.calls if c[0] == "GET")


def test_graph_needs_its_tenant_and_client():
    with pytest.raises(ConfigError, match="GRAPH_TENANT_ID"):
        directory.GraphDirectory(settings(graph_tenant_id=""), resolver=Secrets())


def test_a_graph_add_goes_through_the_ledger_once():
    s = settings()
    store = MemoryStore()
    ctx = ToolContext(settings=s, client=None, store=store, run_id="r", thread_id="wi-1")
    ctx.approval = ApprovalDecision(thread_id="wi-1", approved=True, approver="boss",
                                    plan_hash="h", approved_userids=["AB12345"])
    user = RequestedUser(userid="AB12345",
                         source_work_items=[SourceWorkItem(work_item_id="1001")])
    fake = FakeGraph()
    g = graph(fake)

    async def twice():
        first = await directory.request_group_membership(ctx, user, group="G", domain="D",
                                                         directory=g)
        second = await directory.request_group_membership(ctx, user, group="G", domain="D",
                                                          directory=g)
        return first, second

    first, second = asyncio.run(twice())
    assert first.outcome.value == "ok" and first.detail["via"] == "graph"
    assert second.replayed
    assert sum(1 for c in fake.calls if c[0] == "POST") == 1


# ------------------------------------------------------- GPT, cloud worker

class FakePage:
    reply = "Your request has been submitted correctly. Failed Requests: 0"
    click_raises = False

    def __init__(self):
        self.clicks = 0

    def open_group(self, group):
        pass

    def stage_user(self, userid, domain):
        return True

    def click_modify(self):
        self.clicks += 1
        if FakePage.click_raises:
            raise RuntimeError("tab crashed")
        return FakePage.reply

    def close(self):
        pass

    def start(self):
        pass

    def add_member(self, **kw):
        from alm_worker.gpt import GptSession

        return GptSession.add_member(self, **kw)


@pytest.mark.parametrize("failure", ["crash", "unreadable"])
def test_the_cloud_gpt_worker_never_retries_an_unknown_outcome(monkeypatch, failure):
    from alm_worker import main as gpt_worker

    monkeypatch.setattr(FakePage, "click_raises", failure == "crash")
    monkeypatch.setattr(FakePage, "reply", "Session expired. Please log in.")
    monkeypatch.setattr(gpt_worker, "GptSession", lambda **_kw: FakePage())
    worker = gpt_worker.Worker(settings(ad_directory="gpt"))
    original = FakePage()
    worker.store, worker.session = MemoryStore(), original
    job = {"idempotency_key": "k1", "userid": "AB12345", "group": "G", "domain": "D",
           "run_id": "r", "operation": "ad_group_add"}
    first = worker.process(job)
    again = worker.process(job)       # a redelivery of the same job
    worker._loop.close()
    assert first.detail.get("outcome_unknown") and first.outcome.value == "failed"
    assert again.replayed                       # closed in the ledger: no second Modify
    assert original.clicks == 1                 # Modify was clicked exactly once
    assert worker.session is not original and worker.session.clicks == 0  # fresh browser


def test_gpt_add_member_raises_outcome_unknown_after_modify():
    page = FakePage()
    FakePage.click_raises = True
    try:
        with pytest.raises(OutcomeUnknown, match="after Modify"):
            page.add_member(userid="AB12345", group="G", domain="D")
    finally:
        FakePage.click_raises = False
