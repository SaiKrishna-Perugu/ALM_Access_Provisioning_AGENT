"""The web console: its locks, and a scripted run driven through the API."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

from fastapi.testclient import TestClient  # noqa: E402
from test_agentic_sandbox import (  # noqa: E402
    FULL_PLAN,
    FULL_SCRIPTS,
    ScriptedLLM,
    sandbox_settings,
)

from alm_agents import sandbox as sandbox_module  # noqa: E402
from alm_agents import web  # noqa: E402

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
TOKEN = "t" * 43
BOTH = "Process work items 1001 and 1002"


def make_manager(environment: str = "TEST") -> web.RunManager:
    def settings_for(mode):
        return sandbox_settings().model_copy(
            update={"shadow_mode": mode != "commit", "environment": environment})

    def llm_for(_settings):
        fake = ScriptedLLM(FULL_PLAN, FULL_SCRIPTS)
        return fake, fake

    return web.RunManager(sandbox=True, settings_for=settings_for, llm_for=llm_for,
                          operator="tester")


def make_client(manager=None, environment: str = "TEST") -> TestClient:
    app = web.create_app(manager or make_manager(environment), port=PORT, access_token=TOKEN,
                         environment=environment, model="scripted", orchestration="agentic")
    return TestClient(app, base_url=f"http://{HOST}")


def signed_in(client: TestClient) -> str:
    response = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert response.status_code == 303
    return client.get("/api/session").json()["csrf"]


def post(client, path, csrf, body):
    return client.post(path, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})


def wait_for(client, run_id, statuses, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} stuck in {run['status']}")


@pytest.fixture(autouse=True)
def sandbox_out(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module, "OUT_DIR", tmp_path)


# ------------------------------------------------------------------ the locks

def test_without_the_link_the_page_is_locked():
    client = make_client()
    response = client.get("/")
    assert response.status_code == 401 and "Sign in from the terminal" in response.text
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/runs").status_code == 401


def test_a_wrong_token_does_not_sign_in():
    client = make_client()
    response = client.get("/?token=nope", follow_redirects=False)
    assert response.status_code == 401
    assert "alm_session" not in response.headers.get("set-cookie", "")


def test_the_link_sets_a_strict_cookie_and_drops_the_token():
    client = make_client()
    response = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    page = client.get("/")
    assert page.status_code == 200 and "ALM Access Console" in page.text


def test_a_foreign_host_header_is_refused():
    """DNS rebinding: a hostile name that resolves to 127.0.0.1."""
    client = make_client()
    signed_in(client)
    response = client.get("/api/session", headers={"Host": f"evil.example:{PORT}"})
    assert response.status_code == 400


def test_writes_need_the_origin_and_the_csrf_token():
    client = make_client()
    csrf = signed_in(client)
    body = {"prompt": "dry run 1001", "mode": "dry"}
    assert client.post("/api/runs", json=body).status_code == 403
    assert client.post("/api/runs", json=body,
                       headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/runs", json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post("/api/runs", json=body,
                       headers={"Origin": ORIGIN, "X-CSRF-Token": csrf + "x"}).status_code == 403


def test_every_response_carries_the_security_headers():
    client = make_client()
    for response in (client.get("/"), client.get("/static/app.js"),
                     client.get("/api/session")):
        for name, value in web.SECURITY_HEADERS.items():
            assert response.headers[name] == value
    assert "script-src 'self'" in web.SECURITY_HEADERS["Content-Security-Policy"]


def test_only_the_two_assets_are_served():
    client = make_client()
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/index.html").status_code == 404
    assert client.get("/static/..%2Fweb.py").status_code == 404


def test_the_page_has_no_inline_script_or_style():
    html = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "<style" not in html and " style=" not in html
    assert "innerHTML" not in (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the request

def test_work_items_come_from_the_numbers_in_the_words():
    assert web.parse_request("dry run 1001, 1002 and 1001", "dry", "", "TEST") == ["1001", "1002"]
    assert web.parse_request("check the queue", "dry", "", "TEST") == []
    # Not work items: parts of other tokens, or too short.
    assert web.parse_request("AB12345 on v1.2 room 42 wi-1003", "dry", "", "TEST") == []


@pytest.mark.parametrize(("prompt", "confirm", "environment", "message"), [
    ("", "COMMIT", "TEST", "Describe"),
    ("x" * (web.MAX_PROMPT + 1), "COMMIT", "TEST", "under"),
    ("provision everyone in the queue", "COMMIT", "TEST", "name its work items"),
    ("1001 1002 1003 1004 1005 1006", "COMMIT", "TEST", "at most 5"),
    ("provision 1001", "", "TEST", "Type COMMIT"),
    ("provision 1001", "COMMIT", "PROD", "Type PROD"),
])
def test_a_writing_run_must_be_scoped_and_confirmed(prompt, confirm, environment, message):
    with pytest.raises(web.RequestRefused, match=message):
        web.parse_request(prompt, "commit", confirm, environment)


def test_the_words_cannot_turn_a_dry_run_into_a_write():
    assert web.parse_request("COMMIT and write everything for 1001", "dry", "", "TEST") == ["1001"]


def test_a_refused_request_is_a_422_with_the_reason():
    client = make_client()
    csrf = signed_in(client)
    response = post(client, "/api/runs", csrf, {"prompt": "write all", "mode": "commit",
                                                "confirm": "COMMIT"})
    assert response.status_code == 422 and "work items" in response.json()["error"]
    response = post(client, "/api/runs", csrf, {"prompt": "1001", "mode": "delete"})
    assert response.status_code == 422


# -------------------------------------------------------------------- the runs

def test_a_dry_run_previews_the_card_and_writes_nothing():
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    started = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "dry"})
    assert started.status_code == 201
    run = wait_for(client, started.json()["id"], {"done", "failed"})
    assert run["status"] == "done", run["error"]
    assert run["work_items"] == ["1001", "1002"]
    events = manager.runs[run["id"]].events
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "approval_preview" in kinds and "approval_required" not in kinds
    assert run["report"]["results"] == [] or all(
        r["outcome"] != "ok" for r in run["report"]["results"])


def test_a_writing_run_waits_for_the_browser_and_writes_only_chosen_users():
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    started = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "commit",
                                               "confirm": "COMMIT"})
    assert started.status_code == 201
    run_id = started.json()["id"]
    run = wait_for(client, run_id, {"awaiting_approval", "done", "failed"})
    assert run["status"] == "awaiting_approval", run["error"]
    shown = [i["userid"] for i in run["pending"]["items"]]
    assert "AB12345" in shown

    # One run at a time.
    busy = post(client, "/api/runs", csrf, {"prompt": "dry run 1001", "mode": "dry"})
    assert busy.status_code == 422 and "already in progress" in busy.json()["error"]
    # A user who was not on the card is ignored; approving nobody is refused.
    assert post(client, f"/api/runs/{run_id}/decision", csrf,
                {"approved": True, "userids": ["ZZ99999"]}).status_code == 409
    assert post(client, f"/api/runs/{run_id}/decision", csrf,
                {"approved": True, "userids": ["AB12345", "ZZ99999"]}).status_code == 200

    run = wait_for(client, run_id, {"done", "failed"})
    assert run["status"] == "done", run["error"]
    decided = [e for e in manager.runs[run_id].events if e["kind"] == "approval_decided"]
    assert decided[0]["data"] == {"approved": True, "userids": ["AB12345"],
                                  "approver": "web:tester"}
    written = {r["userid"] for r in run["report"]["results"]
               if r["outcome"] == "ok" and not r["replayed"]}
    assert written <= {"AB12345"}
    assert "AB12345" in written

    # Too late to decide again.
    assert post(client, f"/api/runs/{run_id}/decision", csrf,
                {"approved": False, "userids": []}).status_code == 409


def test_a_declined_user_does_not_reopen_the_gate_but_a_new_one_does():
    from alm_agents.agentic import needs_approval
    from alm_core.models import ApprovalDecision

    approval = ApprovalDecision(thread_id="t", approved=True, approver="web:tester",
                                plan_hash="h", approved_userids=["AB12345"])
    state = {"approval": approval,
             "approval_request": {"items": [{"userid": "AB12345"}, {"userid": "CD67890"}]},
             "board": {"users": {"AB12345": {}, "CD67890": {}}}}
    assert not needs_approval(state)
    state["board"]["users"]["EF11111"] = {}
    assert needs_approval(state)


def test_a_rejection_in_the_browser_halts_the_run():
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    run_id = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "commit",
                                              "confirm": "COMMIT"}).json()["id"]
    wait_for(client, run_id, {"awaiting_approval"})
    assert post(client, f"/api/runs/{run_id}/decision", csrf,
                {"approved": False, "userids": []}).status_code == 200
    run = wait_for(client, run_id, {"done", "failed"})
    assert run["report"]["halted"] and "rejected" in run["report"]["halt_reason"]


def test_the_event_stream_replays_the_run_and_ends():
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    run_id = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "dry"}).json()["id"]
    wait_for(client, run_id, {"done", "failed"})
    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {\"")]
    assert [e["seq"] for e in events] == list(range(len(manager.runs[run_id].events)))
    assert body.rstrip().endswith("event: end\ndata: {}")
    after = client.get(f"/api/runs/{run_id}/events?after={events[-2]['seq']}").text
    assert after.count("data: {\"seq\"") == 1


def test_a_failed_run_shows_a_scrubbed_error():
    secret = "AIza" + "1" * 35  # pragma: allowlist secret - a fake key

    def llm_for(_settings):
        raise RuntimeError(f"model refused key {secret}")

    manager = web.RunManager(sandbox=True, settings_for=lambda m: sandbox_settings(),
                             llm_for=llm_for, operator="tester")
    client = make_client(manager)
    csrf = signed_in(client)
    run_id = post(client, "/api/runs", csrf, {"prompt": "dry run 1001", "mode": "dry"}).json()["id"]
    run = wait_for(client, run_id, {"done", "failed"})
    assert run["status"] == "failed" and secret not in run["error"]


# ------------------------------------------------------------ stop and trace

def test_the_session_says_the_data_is_simulated():
    client = make_client()
    signed_in(client)
    session = client.get("/api/session").json()
    assert session["data_source"] == "simulated" and session["sandbox"] is True


def test_the_stop_button_ends_a_run_waiting_at_the_card_without_writing():
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    run_id = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "commit",
                                              "confirm": "COMMIT"}).json()["id"]
    wait_for(client, run_id, {"awaiting_approval"})
    assert client.post(f"/api/runs/{run_id}/stop", json={},
                       headers={"Origin": ORIGIN}).status_code == 403
    answer = post(client, f"/api/runs/{run_id}/stop", csrf, {})
    assert answer.status_code == 200 and answer.json()["status"] == "stopping"
    run = wait_for(client, run_id, {"stopped", "done", "failed"})
    assert run["status"] == "stopped", run
    assert run["report"]["halt_reason"] == "stopped by web:tester"
    assert run["report"]["results"] == [] and run["stopped_by"] == "web:tester"
    kinds = [e["kind"] for e in manager.runs[run_id].events]
    assert "stop_requested" in kinds and "approval_decided" not in kinds
    assert post(client, f"/api/runs/{run_id}/stop", csrf, {}).status_code == 409


def test_a_stop_file_from_another_terminal_stops_a_web_run(tmp_path, monkeypatch):
    from alm_agents import local
    from alm_agents.control import stop_file_for, write_stop_file

    monkeypatch.setattr(local, "DEFAULT_LEDGER", tmp_path / "local" / "alm.db")
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    run_id = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "commit",
                                              "confirm": "COMMIT"}).json()["id"]
    wait_for(client, run_id, {"awaiting_approval"})
    write_stop_file(stop_file_for(str(tmp_path / "local" / "alm.db")), by="cli:ops")
    run = wait_for(client, run_id, {"stopped", "done", "failed"})
    assert run["status"] == "stopped" and run["report"]["halt_reason"] == "stopped by cli:ops"


def test_the_trace_tab_serves_the_runs_calls_and_the_file(tmp_path):
    manager = make_manager()
    client = make_client(manager)
    csrf = signed_in(client)
    run_id = post(client, "/api/runs", csrf, {"prompt": BOTH, "mode": "dry"}).json()["id"]
    wait_for(client, run_id, {"done", "failed"})

    everything = client.get(f"/api/runs/{run_id}/trace").json()
    services = {r["service"] for r in everything["records"]}
    # A dry run never reaches the ledger: the policy refuses its writes first.
    assert {"run", "supervisor", "tool", "ewm", "jts", "approval"} <= services
    assert everything["done"] and str(tmp_path) in everything["path"]
    started = everything["records"][0]
    assert started["kind"] == "started" and started["data_source"] == "simulated"

    tools = client.get(f"/api/runs/{run_id}/trace?service=tool").json()["records"]
    assert tools and {r["service"] for r in tools} == {"tool"}
    # One trace record per tool call - the page's console must not add a second.
    assert len(tools) == sum(e["kind"] == "tool_call" for e in manager.runs[run_id].events)
    tail = client.get(f"/api/runs/{run_id}/trace?after={everything['next'] - 1}").json()
    assert len(tail["records"]) == 1

    download = client.get(f"/api/runs/{run_id}/trace.jsonl")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    lines = [json.loads(line) for line in download.text.splitlines() if line.strip()]
    assert len(lines) == len(everything["records"])
    assert client.get("/api/runs/nope/trace").status_code == 404
