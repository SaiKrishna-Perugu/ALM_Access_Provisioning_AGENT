"""The API only queues: triggers, decisions and stops become store rows.

Two app instances over one store stand in for two replicas behind a load
balancer: a delivery seen by one is refused by the other, and a stop sent to
either reaches the run.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from alm_api import main  # noqa: E402
from alm_api.security import sign_payload  # noqa: E402
from alm_core.store.memory import MemoryStore  # noqa: E402

SECRET = "webhook-secret-for-tests"  # pragma: allowlist secret
USER = {"x-goog-authenticated-user-email": "accounts.google.com:ops@example.com"}


@pytest.fixture
def api(monkeypatch):
    store = MemoryStore()
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    monkeypatch.setattr(main.runtime, "services", SimpleNamespace(store=store))
    monkeypatch.setattr(main.runtime, "settings", SimpleNamespace(
        webhook_secret_name="webhook", approval_signing_secret_name="signing",  # pragma: allowlist secret
        shadow_mode=False, environment="TEST", auth_mode="iap",
        role_map='{"ops@example.com": "admin", "viewer@example.com": "viewer"}'))
    monkeypatch.setattr(main.runtime, "resolver", SimpleNamespace(get=lambda _n: SECRET))
    # No `with`: the lifespan (services, embedded workers) does not run.
    return TestClient(main.app), store


def delivery(work_item="1001", delivery_id="d-1"):
    body = f'{{"work_item_id": "{work_item}"}}'.encode()
    ts = str(time.time())
    return body, {"x-alm-signature": sign_payload(SECRET, f"{ts}.".encode() + body),
                  "x-alm-timestamp": ts, "x-alm-delivery": delivery_id,
                  "content-type": "application/json"}


def test_a_webhook_queues_a_run_and_a_replay_is_refused_by_any_replica(api):
    client, store = api
    body, headers = delivery()
    first = client.post("/webhooks/ewm", content=body, headers=headers)
    other_replica = TestClient(main.app)          # same store, another instance
    replay = other_replica.post("/webhooks/ewm", content=body, headers=headers)
    assert first.status_code == 202 and first.json()["queued"] is True
    assert replay.status_code == 401
    jobs = list(store._jobs)
    assert [(j["kind"], j["thread_id"], j["payload"]["mode"]) for j in jobs] == [
        ("start", "wi-1001", "commit")]


def test_a_second_delivery_for_a_queued_work_item_does_not_queue_twice(api):
    client, store = api
    for n in (1, 2):
        body, headers = delivery(delivery_id=f"d-{n}")
        assert client.post("/webhooks/ewm", content=body, headers=headers).status_code == 202
    assert len(store._jobs) == 1


def test_a_bad_signature_or_work_item_id_is_refused(api):
    client, _store = api
    body, headers = delivery()
    headers["x-alm-signature"] = "nope"
    assert client.post("/webhooks/ewm", content=body, headers=headers).status_code == 401
    body, headers = delivery(work_item="1; DROP")
    assert client.post("/webhooks/ewm", content=body, headers=headers).status_code == 422


VIEWER = {"x-goog-authenticated-user-email": "accounts.google.com:viewer@example.com"}


def test_runs_and_queue_come_from_the_store(api):
    import asyncio

    client, store = api
    assert client.get("/runs").status_code == 401          # nobody signed in
    client.headers.update(USER)
    asyncio.run(store.upsert_run("wi-7", status="running", mode="dry", scope=["7"]))
    asyncio.run(store.record_trace("wi-7", [{"seq": 0, "service": "run", "kind": "started"}]))
    runs = client.get("/runs").json()["runs"]
    one = client.get("/runs/wi-7").json()
    trace = client.get("/runs/wi-7/trace").json()
    assert [r["thread_id"] for r in runs] == ["wi-7"] and one["status"] == "running"
    assert one["approval"] is None and one["events"] == []
    assert trace["records"][0]["kind"] == "started" and trace["next"] == 1
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/queue").json() == {"depth": {}, "dead": []}


def test_stop_needs_a_caller_and_reaches_the_run(api):
    import asyncio

    client, store = api
    asyncio.run(store.upsert_run("wi-7", status="running"))
    assert client.post("/runs/wi-7/stop", json={}).status_code == 401
    answer = client.post("/runs/wi-7/stop", json={"reason": "wrong item"}, headers=USER)
    assert answer.status_code == 202 and answer.json()["status"] == "stopping"
    assert asyncio.run(store.stop_request("wi-7"))["by"] == "ops@example.com"
    assert client.post("/runs/nope/stop", json={}, headers=USER).status_code == 404


def test_a_manual_sweep_needs_a_caller(api):
    client, store = api
    assert client.post("/admin/reconcile").status_code == 401
    answer = client.post("/admin/reconcile", headers=USER)
    assert answer.status_code == 202
    assert store._jobs[0]["thread_id"] == answer.json()["thread_id"]
    assert store._runs[answer.json()["thread_id"]]["requested_by"] == "ops@example.com"


def test_roles_decide_who_may_stop_start_and_sweep(api):
    import asyncio

    client, store = api
    asyncio.run(store.upsert_run("wi-7", status="running"))
    assert client.get("/runs", headers=VIEWER).status_code == 200
    assert client.post("/runs/wi-7/stop", json={}, headers=VIEWER).status_code == 403
    assert client.post("/admin/reconcile", headers=VIEWER).status_code == 403
    assert client.post("/runs", json={"prompt": "dry run 1001"},
                       headers=VIEWER).status_code == 403


def test_an_operator_starts_runs_from_plain_words(api):
    client, store = api
    started = client.post("/runs", json={"prompt": "Dry run work item 1001"}, headers=USER)
    assert started.status_code == 201
    assert started.json() == {"thread_id": "wi-1001", "work_items": ["1001"], "mode": "dry"}
    assert store._runs["wi-1001"]["requested_by"] == "ops@example.com"
    again = client.post("/runs", json={"prompt": "1001 once more"}, headers=USER)
    assert again.status_code == 409
    unscoped = client.post("/runs", json={"prompt": "write everything", "mode": "commit",
                                          "confirm": "COMMIT"}, headers=USER)
    assert unscoped.status_code == 422 and "work items" in unscoped.json()["detail"]
    two = client.post("/runs", json={"prompt": "provision 1002 and 1003", "mode": "commit",
                                     "confirm": "COMMIT"}, headers=USER)
    assert two.status_code == 201 and two.json()["thread_id"].startswith("console-")


def test_traces_are_masked_for_everyone_but_auditors(api, monkeypatch):
    import asyncio

    client, store = api
    asyncio.run(store.upsert_run("wi-7", status="running"))
    asyncio.run(store.record_trace("wi-7", [{"seq": 0, "service": "tool",
                                             "observation": "alice.smith@example.com AB12345"}]))
    seen = client.get("/runs/wi-7/trace", headers=USER).json()["records"][0]   # admin
    masked = client.get("/runs/wi-7/trace", headers=VIEWER).json()["records"][0]
    assert "alice.smith@example.com" in seen["observation"]
    assert "alice.smith" not in masked["observation"] and "AB12345" in masked["observation"]
