"""Stopping a run safely, and the trace that records every call it makes."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from langchain_core.messages import AIMessage  # noqa: E402
from test_agentic_sandbox import (  # noqa: E402
    FULL_PLAN,
    FULL_SCRIPTS,
    ScriptedLLM,
    approve,
    sandbox_settings,
)
from test_local_runner import local_settings  # noqa: E402

from alm_agents import llm as llm_module  # noqa: E402
from alm_agents import local  # noqa: E402
from alm_agents.control import RunControl, stop_file_for, write_stop_file  # noqa: E402
from alm_agents.runner import Console  # noqa: E402
from alm_agents.sandbox import SandboxBackend, SandboxEstate, run_sandbox  # noqa: E402
from alm_agents.trace import (  # noqa: E402
    RunTrace,
    TracedModel,
    format_record,
    read_trace,
)
from alm_core import trace as core_trace  # noqa: E402


class Watch(Console):
    """A console that can pull the Stop switch when it sees a given event."""

    def __init__(self, control, when):
        super().__init__()
        self.control = control
        self.when = when
        self.events = []

    def __call__(self, kind, data):
        self.events.append((kind, data))
        if self.when(kind, data):
            self.control.request_stop("test")


class SlowLLM:
    """A model that takes far longer than anyone would wait."""

    def __init__(self):
        self.cancelled = False

    def bind_tools(self, _tools, **_kw):
        return self

    async def ainvoke(self, _messages, **_kw):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return AIMessage(content="too late")


def stop_later(control, seconds):
    timer = threading.Timer(seconds, lambda: control.request_stop("test"))
    timer.start()
    return timer


# ------------------------------------------------------------- the switch

def test_a_stop_file_stops_every_run_or_only_the_one_named(tmp_path):
    path = stop_file_for(str(tmp_path / "alm.db"))
    mine = RunControl(thread_id="web-1", stop_file=path)
    write_stop_file(path, thread_id="local-2", by="cli:ops")
    assert not mine.stop_requested()
    mine._last_poll = 0
    write_stop_file(path, by="cli:ops")
    assert mine.stop_requested() and mine.by == "cli:ops"
    assert mine.reason == "stopped by cli:ops"


def test_a_stop_file_left_from_an_earlier_run_is_ignored(tmp_path):
    path = stop_file_for(str(tmp_path / "alm.db"))
    write_stop_file(path)
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert not RunControl(thread_id="t", stop_file=path).stop_requested()


# ------------------------------------------------------------ in the graph

def test_a_stop_lets_the_write_in_progress_finish_and_starts_nothing_more(tmp_path):
    """Stopped right after AB12345 is imported: CD67890 and TB22322, queued in
    the same model turn, are never touched, and nothing after the provisioner runs."""
    control = RunControl()
    first_write = lambda kind, d: (kind == "tool_call" and d["tool"] == "provision_jts_user"  # noqa: E731
                                   and not d["denied"])
    watch = Watch(control, first_write)
    estate = SandboxEstate.default()
    report = asyncio.run(run_sandbox(
        sandbox_settings(), llm=ScriptedLLM(FULL_PLAN, FULL_SCRIPTS), estate=estate,
        console=watch, decide=approve, shots_dir=str(tmp_path), control=control))

    assert report["halted"] and report["halt_reason"] == "stopped by test"
    written = [(r["userid"], r["operation"], r["outcome"]) for r in report["results"]]
    assert ("AB12345", "jts_create", "ok") in written
    assert not [w for w in written if w[0] in ("CD67890", "TB22322")]
    assert estate.ad_requests == [] and estate.comments["1001"] == []
    tools = [d["tool"] for k, d in watch.events if k == "tool_call"]
    assert tools[-1] == "provision_jts_user"
    assert ("stopped", {"reason": "stopped by test", "at": "after the provisioner agent",
                        "by": "test"}) in watch.events


def test_a_stop_abandons_a_slow_agent_model_call(tmp_path):
    control = RunControl()
    slow = SlowLLM()
    timer = stop_later(control, 0.5)
    started = time.monotonic()
    report = asyncio.run(run_sandbox(
        sandbox_settings(), llm=slow, supervisor_llm=ScriptedLLM(["triage"], {}),
        console=Console(), decide=approve, shots_dir=str(tmp_path), control=control))
    timer.cancel()
    assert time.monotonic() - started < 10
    assert report["halted"] and report["halt_reason"] == "stopped by test"
    assert slow.cancelled


def test_a_stop_abandons_a_slow_routing_decision(tmp_path):
    control = RunControl()
    timer = stop_later(control, 0.5)
    started = time.monotonic()
    report = asyncio.run(run_sandbox(
        sandbox_settings(), llm=ScriptedLLM([], {}), supervisor_llm=SlowLLM(),
        console=Console(), decide=approve, shots_dir=str(tmp_path), control=control))
    timer.cancel()
    assert time.monotonic() - started < 10
    assert report["halted"] and report["halt_reason"] == "stopped by test"
    assert report["results"] == []


def test_a_stop_at_the_approval_card_writes_nothing(tmp_path):
    control = RunControl()

    def stop_then_approve(payload):
        control.request_stop("test")
        return approve(payload)

    estate = SandboxEstate.default()
    report = asyncio.run(run_sandbox(
        sandbox_settings(), llm=ScriptedLLM(FULL_PLAN, FULL_SCRIPTS), estate=estate,
        console=Console(), decide=stop_then_approve, shots_dir=str(tmp_path),
        control=control))
    assert report["halted"] and report["halt_reason"] == "stopped by test"
    assert report["results"] == [] and report["policy"]["writes"] == 0


# ------------------------------------------------------------------ trace

def test_the_trace_records_every_kind_of_call_and_no_secrets(tmp_path):
    trace = RunTrace(tmp_path / "t.jsonl", thread_id="t1",
                     hosts={"ewm": "ewm.example.test", "jts": "jts.example.test"})
    key = "AIza" + "2" * 35  # pragma: allowlist secret - a fake key
    model = TracedModel(ScriptedLLM(FULL_PLAN, FULL_SCRIPTS), role="agent")
    with trace:
        estate = SandboxEstate.default()
        asyncio.run(run_sandbox(
            sandbox_settings(), llm=model, estate=estate, console=Console(), decide=approve,
            shots_dir=str(tmp_path / "shots"), trace=trace))
        core_trace.emit("http", "request", method="GET", url="https://ewm.example.test/ccm/x",
                        status=200)
        core_trace.emit("log", "leak", text=f"key={key}")
    assert not core_trace.active()  # the sink is removed when the run ends

    records = read_trace(tmp_path / "t.jsonl")
    services = {r["service"] for r in records}
    assert {"supervisor", "tool", "model", "ewm", "jts", "ledger", "approval", "http"} <= services
    assert [r["seq"] for r in records] == list(range(len(records)))
    http = next(r for r in records if r["service"] == "http")
    assert http["system"] == "ewm"
    writes = [r for r in records if r["service"] == "ledger" and r["kind"] == "write"]
    assert writes and all("ms" in r and "operation" in r for r in writes)
    assert key not in (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert all(isinstance(format_record(r), str) for r in records)


def test_the_http_hook_keeps_secrets_out_of_urls():
    import requests

    seen = []
    core_trace.set_sink(seen.append)
    try:
        response = requests.Response()
        response.status_code = 302
        response.url = "https://jts.example.test/jts/j_security_check?j_password=hunter2&x=1"
        response.request = requests.Request("POST", response.url).prepare()
        response.headers["Content-Length"] = "0"
        core_trace.http_response(response)
    finally:
        core_trace.set_sink(None)
    assert seen[0]["method"] == "POST" and seen[0]["status"] == 302
    assert "hunter2" not in seen[0]["url"] and "x=1" in seen[0]["url"]


def test_a_traced_model_records_failures_and_stays_transparent():
    class Broken:
        model = "gemini-test"

        async def ainvoke(self, _messages, **_kw):
            raise RuntimeError("quota exceeded")

    seen = []
    core_trace.set_sink(seen.append)
    try:
        wrapped = TracedModel(Broken(), role="agent")
        assert wrapped.model == "gemini-test"
        with pytest.raises(RuntimeError):
            asyncio.run(wrapped.ainvoke([("system", "You are the triage agent."),
                                         ("human", "go")]))
    finally:
        core_trace.set_sink(None)
    assert seen[0]["service"] == "model" and seen[0]["ok"] is False
    assert seen[0]["caller"] == "triage" and "quota" in seen[0]["error"]


def test_log_lines_reach_the_trace_even_when_the_console_is_quiet(tmp_path):
    from alm_core.logging import get_logger, route_console

    route_console("ERROR")
    trace = RunTrace(tmp_path / "log.jsonl", thread_id="t")
    with trace:
        get_logger("alm.test").info("policy_denied", tool="provision_jts_user")
    records = read_trace(tmp_path / "log.jsonl")
    assert any(r["service"] == "log" and r["kind"] == "policy_denied"
               and r.get("tool") == "provision_jts_user" for r in records)


# ------------------------------------------------------------- local runs

class _FakeJazz:
    def __init__(self, *_a):
        pass

    def session(self, *_a, **_k):
        return None

    def close(self):
        pass


class _Backend(SandboxBackend):
    def close(self):
        pass


def _local_run(tmp_path, monkeypatch, *, control=None):
    settings = local_settings(tmp_path, commit=False, orchestration="guided")
    monkeypatch.setattr(local, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr("alm_core.auth.JazzClient", _FakeJazz)
    monkeypatch.setattr(local, "make_local_backend",
                        lambda **_: _Backend(SandboxEstate.default()))
    scripted = ScriptedLLM([], {"triage": [[("fetch_open_requests", {"limit": 10})]]})
    monkeypatch.setattr(llm_module, "get_agent_llm", lambda _s: scripted)
    monkeypatch.setattr(llm_module, "get_supervisor_llm", lambda _s: scripted)
    args = SimpleNamespace(work_item=["1001"], resume="", record=False, auto_approve=False)
    return asyncio.run(local.run(settings, args, Console(), resolver=object(),
                                 control=control))


def test_a_terminal_run_writes_its_own_trace(tmp_path, monkeypatch):
    report = _local_run(tmp_path, monkeypatch)
    records = read_trace(report["trace"])
    assert records[0]["service"] == "run" and records[0]["kind"] == "started"
    assert records[-1]["service"] == "run" and records[-1]["kind"] == "finished"
    assert any(r["service"] == "ewm" and r["kind"] == "fetch_work_item" for r in records)
    assert str(tmp_path / "out" / "traces") in report["trace"]


def test_a_terminal_run_stops_when_asked(tmp_path, monkeypatch):
    control = RunControl()
    control.request_stop("cli:ops (Ctrl+C)")
    report = _local_run(tmp_path, monkeypatch, control=control)
    assert report["halted"] and report["halt_reason"] == "stopped by cli:ops (Ctrl+C)"
    finished = read_trace(report["trace"])[-1]
    assert finished["halt_reason"] == "stopped by cli:ops (Ctrl+C)"


def test_the_cli_flags_for_stop_and_trace():
    args = local.parse_args(["--stop"])
    assert args.stop == "all"
    assert local.parse_args(["--stop", "local-1234"]).stop == "local-1234"
    assert local.parse_args(["--trace", "last", "--follow"]).follow
    with pytest.raises(SystemExit):
        local.parse_args(["--follow"])


def test_show_trace_prints_a_line_per_record(tmp_path):
    settings = local_settings(tmp_path)
    folder = tmp_path / "local" / "traces"
    folder.mkdir(parents=True)
    lines = [{"seq": 0, "at": "2026-09-30T10:00:00.000+00:00", "t": 0.0, "service": "run",
              "kind": "started"},
             {"seq": 1, "at": "2026-09-30T10:00:01.000+00:00", "t": 1.0, "service": "http",
              "method": "GET", "url": "https://ewm/x", "status": 200, "ms": 40}]
    (folder / "local-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines),
                                          encoding="utf-8")

    class Lines(Console):
        def __init__(self):
            super().__init__()
            self.out = []

        def line(self, text=""):
            self.out.append(text)

    console = Lines()
    assert local.show_trace(settings, console, "local-1") == 0
    assert len(console.out) == 3 and "GET https://ewm/x -> 200" in console.out[2]
    assert local.show_trace(settings, Lines(), "nope") == 2


def test_the_model_providers_http_calls_are_traced_as_http(tmp_path):
    import logging

    from alm_core.logging import route_console

    route_console("ERROR")
    trace = RunTrace(tmp_path / "h.jsonl", thread_id="t")
    with trace:
        logging.getLogger("httpx").info(
            'HTTP Request: POST https://aiplatform.googleapis.com/v1/models/x:generateContent'
            '?key=secret "HTTP/1.1 429 Too Many Requests"')
    record = read_trace(tmp_path / "h.jsonl")[0]
    assert record["service"] == "http" and record["status"] == 429
    assert record["system"] == "aiplatform.googleapis.com" and "secret" not in record["url"]
