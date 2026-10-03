"""The hosted console end to end: an operator starts a run in the page, a worker
drives it, it parks at the card, two approvers decide, a worker finishes it -
all through the console's /api, as the browser would.

One event loop for the API (httpx's ASGI transport) and the workers, over the
SQLite store the tests share with tests/test_worker.py.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("langgraph")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

import httpx  # noqa: E402
from test_worker import run_with  # noqa: E402

from alm_api import main  # noqa: E402

BASE = "https://alm.example.com"
ROLES = {"ops@example.com": "operator", "alice@example.com": "approver",
         "bob@example.com": "approver", "view@example.com": "viewer"}


def who(email):
    return {"x-goog-authenticated-user-email": f"accounts.google.com:{email}"}


OPS, ALICE, BOB, VIEW = (who(e) for e in ("ops@example.com", "alice@example.com",
                                           "bob@example.com", "view@example.com"))


def hosted(h, monkeypatch, **extra):
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    monkeypatch.setattr(main.runtime, "services", h.services)
    monkeypatch.setattr(main.runtime, "settings", h.services.settings.model_copy(update={
        "auth_mode": "iap", "role_map": json.dumps(ROLES), "approval_base_url": BASE,
        **extra}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url=BASE)


async def events(client, thread_id):
    response = await client.get(f"/api/runs/{thread_id}/events", headers=VIEW)
    return [json.loads(line[6:])["kind"] for line in response.text.splitlines()
            if line.startswith("data: {\"seq\"")]


def test_a_writing_run_from_the_page_needs_two_approvers_in_prod(tmp_path, monkeypatch):
    async def scenario(h):
        async with hosted(h, monkeypatch, environment="PROD") as client:
            started = await client.post("/api/runs", headers=OPS, json={
                "prompt": "Provision the users on work item 1001", "mode": "commit",
                "confirm": "PROD"})
            assert started.status_code == 201, started.text
            thread = started.json()["id"]
            await h.worker("w1").drain()

            parked = (await client.get(f"/api/runs/{thread}", headers=ALICE)).json()
            first = await client.post(f"/api/runs/{thread}/decision", headers=ALICE,
                                      json={"approved": True, "userids": ["AB12345"]})
            self_vote = await client.post(f"/api/runs/{thread}/decision", headers=ALICE,
                                          json={"approved": True, "userids": ["AB12345"]})
            await h.worker("w2").drain()            # nothing to do yet: one vote of two
            waiting = (await client.get(f"/api/runs/{thread}", headers=VIEW)).json()
            await client.post(f"/api/runs/{thread}/decision", headers=BOB,
                              json={"approved": True, "userids": ["AB12345", "CD67890"]})
            await h.worker("w3").drain()
            done = (await client.get(f"/api/runs/{thread}", headers=VIEW)).json()
            return parked, first.json(), self_vote, waiting, done, await events(client, thread)

    parked, first, self_vote, waiting, done, kinds = run_with(tmp_path, scenario)
    assert parked["status"] == "awaiting_approval"
    assert parked["pending"]["needed"] == 2 and parked["pending"]["can_vote"]
    assert first["tally"]["complete"] is False
    assert self_vote.status_code == 403 and "already decided" in self_vote.json()["detail"]
    assert waiting["status"] == "awaiting_approval" and waiting["pending"]["can_vote"] is False
    assert done["status"] == "done", done["error"]
    written = {(r["userid"], r["outcome"]) for r in done["report"]["results"]
               if r["operation"] == "jts_create"}
    assert written == {("AB12345", "ok")}           # only what both approvers ticked
    assert {"run_started", "supervisor", "tool_call", "approval_required",
            "approval_decided", "run_finished"} <= set(kinds)


def test_a_dry_run_from_the_page_previews_and_finishes(tmp_path, monkeypatch):
    async def scenario(h):
        async with hosted(h, monkeypatch) as client:
            started = await client.post("/api/runs", headers=OPS,
                                        json={"prompt": "Dry run work item 1001"})
            thread = started.json()["id"]
            await h.worker("w").drain()
            run = (await client.get(f"/api/runs/{thread}", headers=VIEW)).json()
            trace = (await client.get(f"/api/runs/{thread}/trace", headers=VIEW)).json()
            download = await client.get(f"/api/runs/{thread}/trace.jsonl", headers=VIEW)
            return run, await events(client, thread), trace, download

    run, kinds, trace, download = run_with(tmp_path, scenario)
    assert run["status"] == "done" and run["mode"] == "dry"
    assert "approval_preview" in kinds and "approval_required" not in kinds
    assert trace["done"] and trace["records"]
    assert not any("@example.com" in json.dumps(r) for r in trace["records"])  # masked
    assert download.headers["content-disposition"].endswith('.jsonl"')


def test_the_page_and_its_api_respect_roles(tmp_path, monkeypatch):
    async def scenario(h):
        async with hosted(h, monkeypatch) as client:
            page_ok = await client.get("/", headers=VIEW)
            page_anon = await client.get("/")
            page_norole = await client.get("/", headers=who("stranger@example.com"))
            session = (await client.get("/api/session", headers=VIEW)).json()
            viewer_start = await client.post("/api/runs", headers=VIEW,
                                             json={"prompt": "dry run 1001"})
            cross_site = await client.post("/api/runs", json={"prompt": "dry run 1001"},
                                           headers={**OPS, "origin": "https://evil.example"})
            static = await client.get("/static/app.js")
            secret_file = await client.get("/static/index.html")
            return (page_ok, page_anon, page_norole, session, viewer_start, cross_site,
                    static, secret_file)

    page_ok, anon, norole, session, viewer_start, cross_site, static, other = run_with(
        tmp_path, scenario)
    assert page_ok.status_code == 200 and "script-src 'self'" in page_ok.headers[
        "content-security-policy"]
    assert anon.status_code == 401 and "No access yet" in anon.text
    assert norole.status_code == 403
    assert session["roles"] == ["viewer"] and session["hosted"] and not session["sandbox"]
    assert viewer_start.status_code == 403
    assert cross_site.status_code == 403 and "another site" in cross_site.json()["detail"]
    assert static.status_code == 200 and other.status_code == 404


def test_stopping_from_the_page_reaches_a_parked_run(tmp_path, monkeypatch):
    async def scenario(h):
        async with hosted(h, monkeypatch) as client:
            thread = (await client.post("/api/runs", headers=OPS, json={
                "prompt": "Provision 1001", "mode": "commit", "confirm": "COMMIT"})).json()["id"]
            await h.worker("w1").drain()
            viewer = await client.post(f"/api/runs/{thread}/stop", headers=VIEW)
            stopped = await client.post(f"/api/runs/{thread}/stop", headers=OPS)
            await h.worker("w2").drain()
            return viewer, stopped.json(), (await client.get(f"/api/runs/{thread}",
                                                             headers=VIEW)).json()

    viewer, stopped, run = run_with(tmp_path, scenario)
    assert viewer.status_code == 403
    assert stopped["status"] == "stopping"
    assert run["status"] == "stopped" and run["stopped_by"] == "ops@example.com"
    assert run["report"]["results"] == []
