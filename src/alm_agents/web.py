"""The web console: type a request, watch the agents work, approve in the browser.

    python src/agent_web.py              # real EWM/JTS (client network); asks the Jazz password once
    python src/agent_web.py --sandbox    # simulated estate, real model - works anywhere

What the browser can and cannot do is decided here, not in the page:

* **Local only.** The server binds 127.0.0.1 and refuses any other Host
  header, so another machine - or a web page using DNS rebinding - cannot
  reach it.
* **A one-time key.** The URL printed at start-up carries a random token. It
  is exchanged for an HttpOnly, SameSite=Strict session cookie and then
  dropped from the address bar. No token, no page.
* **Writes need proof of origin.** Every POST must come from this page: its
  Origin must be this server and it must carry the session's CSRF token.
* **The prompt cannot widen a run.** Work item numbers are read from the
  request by code, write mode comes from an explicit switch, and a writing run
  must name 1-5 work items and be confirmed by typing COMMIT (and PROD on
  production). The agents see the words as context only.
* **The password never touches the browser.** It is typed in the terminal
  once, when the server starts, and held in memory for the session.
* **The approval is the human's.** A writing run pauses at the gate until
  someone approves in the page, and can approve only users on the card.
* **The page is locked down.** A strict Content-Security-Policy, no third-party
  requests, no framing, and every piece of agent output is rendered as text.
* **Stop means stop, safely.** The Stop button (or ``agent_local.py --stop``
  from another terminal, or Ctrl+C here) halts the run after its current step;
  a write in progress always finishes, so nothing is left half-done.
* **Everything is traced.** Each run records every model, tool, service, HTTP,
  ledger and GPT call to ``out/<local|sandbox>/traces/<thread>.jsonl``; the
  Trace tab shows it live and downloads it.
"""
# No `from __future__ import annotations` here: FastAPI must see the real
# types of the handlers defined inside create_app, not strings it cannot resolve.
import argparse
import asyncio
import getpass
import hmac
import json
import queue
import re
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from .runner import Console

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
LOOPBACK = "127.0.0.1"
MAX_PROMPT = 2000
MAX_COMMIT_WORK_ITEMS = 5
APPROVAL_TIMEOUT_SECONDS = 4 * 3600
SESSION_SECONDS = 12 * 3600
_WORK_ITEM_RE = re.compile(r"(?<![\w-])(\d{4,10})(?![\w-])")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


class RequestRefused(ValueError):
    """A request the console will not start, with the reason shown to the operator."""


# -------------------------------------------------------------- the request

def parse_request(prompt: str, mode: str, confirm: str, environment: str) -> list[str]:
    """Work items for a run, read from the operator's words by code.

    The mode is never inferred from the words: a request that says "commit"
    in a dry run is still a dry run. A writing run must name its work items.
    """
    text = (prompt or "").strip()
    if not text:
        raise RequestRefused("Describe what to do, for example: dry run work item 1001.")
    if len(text) > MAX_PROMPT:
        raise RequestRefused(f"Keep the request under {MAX_PROMPT} characters.")
    work_items = list(dict.fromkeys(_WORK_ITEM_RE.findall(text)))
    if mode == "commit":
        if not work_items:
            raise RequestRefused("A run that writes must name its work items by number.")
        if len(work_items) > MAX_COMMIT_WORK_ITEMS:
            raise RequestRefused(f"A run that writes takes at most {MAX_COMMIT_WORK_ITEMS} "
                                 f"work items ({len(work_items)} named). Split it.")
        needed = "PROD" if environment == "PROD" else "COMMIT"
        if (confirm or "").strip() != needed:
            raise RequestRefused(f"Type {needed} to confirm a run that writes.")
    return work_items


# --------------------------------------------------------------- one run

@dataclass
class WebRun:
    id: str
    thread_id: str
    prompt: str
    mode: str
    work_items: list[str]
    sandbox: bool
    created: float = field(default_factory=time.time)
    # running | awaiting_approval | stopping | done | stopped | failed
    status: str = "starting"
    events: list[dict] = field(default_factory=list)
    pending: dict | None = None
    report: dict | None = None
    error: str = ""
    decisions: queue.Queue = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)
    control: object = None   # alm_agents.control.RunControl
    trace: object = None     # alm_agents.trace.RunTrace
    thread: threading.Thread | None = None

    def emit(self, kind: str, data: dict) -> None:
        with self.lock:
            self.events.append({"seq": len(self.events), "kind": kind,
                                "at": round(time.time(), 3), "data": data})

    def summary(self) -> dict:
        return {"id": self.id, "thread_id": self.thread_id, "prompt": self.prompt,
                "mode": self.mode, "work_items": self.work_items, "sandbox": self.sandbox,
                "status": self.status, "created": self.created, "error": self.error,
                "pending": self.pending, "report": _report_view(self.report),
                "stopped_by": getattr(self.control, "by", "") or "",
                "trace": str(getattr(self.trace, "path", "") or "")}


def _clip(value, limit: int = 1500):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f" ... [{len(text) - limit} more]"


class WebConsole(Console):
    """The runtime's progress hook, turned into events for the page."""

    def __init__(self, run: WebRun):
        super().__init__(verbose=True)
        self.run = run

    def line(self, text: str = "") -> None:
        if text.strip():
            self.run.emit("log", {"text": text})

    def __call__(self, kind: str, data: dict) -> None:
        if kind == "approval_required":
            return  # the decider sends the card, with what the page needs
        if kind == "tool_call":
            data = {**data, "args": {k: _clip(v, 300) for k, v in (data.get("args") or {}).items()},
                    "observation": _clip(data.get("observation", ""))}
        elif kind == "agent_text":
            data = {**data, "text": _clip(data.get("text", ""), 2000)}
        self.run.emit(kind, json.loads(json.dumps(data, default=str)))


def _report_view(report: dict | None) -> dict | None:
    if not report:
        return None
    return {
        "halted": report.get("halted"), "halt_reason": report.get("halt_reason", ""),
        "hops": report.get("hops"), "approval_rounds": report.get("approval_rounds"),
        "metrics": report.get("metrics") or {},
        "results": [{k: r.get(k) for k in ("userid", "operation", "outcome", "work_item_id",
                                          "message", "replayed")}
                    for r in report.get("results") or []],
        "estate": report.get("estate"),
        "trace": report.get("trace", ""),
    }


# ----------------------------------------------------------- the manager

class RunManager:
    """Starts runs one at a time, each in its own thread and event loop."""

    ACTIVE = ("starting", "running", "awaiting_approval", "stopping")

    def __init__(self, *, sandbox: bool, settings_for, llm_for, resolver=None,
                 operator: str = "", out_dir: Path | None = None):
        self.sandbox = sandbox
        self.settings_for = settings_for  # (mode) -> Settings
        self.llm_for = llm_for            # (settings) -> (agent_llm, supervisor_llm)
        self.resolver = resolver
        self.operator = operator or getpass.getuser()
        # Where traces go: out/local for real runs, out/sandbox for simulated ones.
        self.out_dir = out_dir
        self.runs: dict[str, WebRun] = {}
        self._busy = threading.Lock()

    def _out_dir(self) -> Path:
        if self.out_dir is not None:
            return self.out_dir
        if self.sandbox:
            from .sandbox import OUT_DIR as SANDBOX_OUT

            return SANDBOX_OUT
        from .local import OUT_DIR as LOCAL_OUT

        return LOCAL_OUT

    def active(self) -> WebRun | None:
        return next((r for r in self.runs.values() if r.status in self.ACTIVE), None)

    def stop(self, run_id: str, by: str = "") -> WebRun:
        """Stop a run after its current step. A write in progress finishes."""
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in self.ACTIVE:
            raise RequestRefused("This run has already ended.")
        by = by or f"web:{self.operator}"
        if run.control is not None:
            run.control.request_stop(by)
        if run.status != "stopping":
            run.emit("stop_requested", {"by": by})
        run.status = "stopping"
        run.decisions.put((False, []))  # wakes a run waiting at the approval card
        return run

    def shutdown(self, timeout: float = 120.0) -> WebRun | None:
        """On Ctrl+C in the server's terminal: stop the active run and let it end."""
        run = self.active()
        if run is None:
            return None
        self.stop(run.id, by=f"cli:{self.operator} (console closed)")
        if run.thread is not None:
            run.thread.join(timeout)
        return run

    def start(self, prompt: str, mode: str, confirm: str) -> WebRun:
        if self.active() is not None:
            raise RequestRefused("A run is already in progress. Finish or decide it first.")
        settings = self.settings_for(mode)
        work_items = parse_request(prompt, mode, confirm, settings.environment)
        run_id = uuid.uuid4().hex[:12]
        prefix = "web-sandbox" if self.sandbox else "web"
        run = WebRun(id=run_id, thread_id=f"{prefix}-{run_id[:8]}", prompt=prompt.strip(),
                     mode=mode, work_items=work_items, sandbox=self.sandbox)
        from .control import RunControl, stop_file_for
        from .local import DEFAULT_LEDGER
        from .trace import open_run_trace

        ledger = getattr(settings, "ledger_path", "") or str(DEFAULT_LEDGER)
        run.control = RunControl(thread_id=run.thread_id,
                                 stop_file=stop_file_for(ledger if not self.sandbox
                                                         else str(DEFAULT_LEDGER)))
        run.trace = open_run_trace(self._out_dir(), run.thread_id, settings=settings)
        self.runs[run_id] = run
        source = data_source(settings, sandbox=self.sandbox)
        run.emit("run_started", {"mode": mode, "work_items": work_items or "the active queue",
                                 "environment": settings.environment,
                                 "sandbox": self.sandbox, "thread_id": run.thread_id,
                                 **source})
        run.thread = threading.Thread(target=self._run, args=(run, settings), daemon=True,
                                      name=f"alm-run-{run_id}")
        run.thread.start()
        return run

    def decide(self, run_id: str, approved: bool, userids: list[str]) -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status != "awaiting_approval" or run.pending is None:
            raise RequestRefused("This run is not waiting for a decision.")
        shown = {str(i.get("userid")) for i in run.pending.get("items", []) if i.get("userid")}
        chosen = [u for u in dict.fromkeys(userids) if u in shown]
        if approved and not chosen:
            raise RequestRefused("Select at least one user to approve, or reject the batch.")
        run.decisions.put((approved, chosen))

    # ------------------------------------------------------------ the thread

    def _decider(self, run: WebRun, settings):
        from alm_core.models import ApprovalDecision

        def decide(payload: dict):
            card = json.loads(json.dumps(payload, default=str))
            shown = [str(i.get("userid")) for i in payload.get("items", []) if i.get("userid")]
            if settings.shadow_mode:
                # A dry run cannot write: the card is a preview of what a writing
                # run will ask, and the plan carries on.
                run.emit("approval_preview", card)
                return ApprovalDecision(
                    thread_id=payload.get("thread_id", ""), approved=True,
                    approver="dry-run:web-preview", plan_hash=payload.get("plan_hash", ""),
                    comment="dry run: preview of the card a writing run asks for",
                    approved_userids=shown)
            run.pending = card
            run.status = "awaiting_approval"
            run.emit("approval_required", card)
            approved, chosen = False, []
            deadline = time.monotonic() + APPROVAL_TIMEOUT_SECONDS
            while True:
                if run.control is not None and run.control.stop_requested():
                    break  # the approval node sees the stop and halts
                try:
                    approved, chosen = run.decisions.get(timeout=1.0)
                    break
                except queue.Empty:
                    if time.monotonic() > deadline:
                        run.emit("log", {"text": "No decision in time: the batch is rejected."})
                        break
            run.pending = None
            stopping = run.control is not None and run.control.stop_requested()
            run.status = "stopping" if stopping else "running"
            if stopping:
                return ApprovalDecision(
                    thread_id=payload.get("thread_id", ""), approved=False,
                    approver=getattr(run.control, "by", "") or f"web:{self.operator}",
                    plan_hash=payload.get("plan_hash", ""), comment="run stopped")
            run.emit("approval_decided", {"approved": approved, "userids": chosen,
                                          "approver": f"web:{self.operator}"})
            return ApprovalDecision(
                thread_id=payload.get("thread_id", ""), approved=approved,
                approver=f"web:{self.operator}", plan_hash=payload.get("plan_hash", ""),
                comment="decided in the web console", approved_userids=chosen)

        return decide

    def _run(self, run: WebRun, settings) -> None:
        if run.status == "starting":
            run.status = "running"
        console = WebConsole(run)
        trace = run.trace
        with trace:
            trace.write({"service": "run", "kind": "started", "via": "web",
                         "operator": self.operator, "mode": run.mode,
                         "scope": run.work_items or "the whole active queue",
                         "environment": settings.environment,
                         "operator_request": run.prompt,
                         "model": getattr(settings, "agent_model", ""),
                         "orchestration": getattr(settings, "orchestration", ""),
                         **data_source(settings, sandbox=self.sandbox)})
            try:
                agent_llm, supervisor_llm = self.llm_for(settings)
                decide = self._decider(run, settings)
                if self.sandbox:
                    from .sandbox import OUT_DIR as SANDBOX_OUT
                    from .sandbox import run_sandbox

                    report = asyncio.run(run_sandbox(
                        settings, llm=agent_llm, supervisor_llm=supervisor_llm,
                        console=console, decide=decide, work_item_ids=run.work_items,
                        operator_request=run.prompt, thread_id=run.thread_id,
                        shots_dir=str(SANDBOX_OUT / "evidence" / run.thread_id),
                        control=run.control, trace=trace))
                    report["trace"] = str(trace.path)
                else:
                    from . import local

                    args = SimpleNamespace(work_item=run.work_items, resume="", record=False,
                                           auto_approve=False)
                    report = asyncio.run(local.run(
                        settings, args, console, resolver=self.resolver, decide=decide,
                        thread_id=run.thread_id, operator_request=run.prompt,
                        control=run.control, trace=trace))
                run.report = report
                reason = report.get("halt_reason", "") or ""
                stopped = bool(report.get("halted")) and reason.startswith("stopped")
                run.status = "stopped" if stopped else "done"
                trace.write({"service": "run", "kind": "finished",
                             "halted": bool(report.get("halted")), "halt_reason": reason,
                             "metrics": report.get("metrics") or {}})
                run.emit("run_finished", {"halted": bool(report.get("halted")),
                                          "halt_reason": reason, "stopped": stopped,
                                          "metrics": report.get("metrics") or {}})
            except Exception as err:  # noqa: BLE001 - shown in the page, never raised
                import traceback

                from alm_core.logging import scrub_secrets

                run.error = scrub_secrets(f"{type(err).__name__}: {err}")[:600]
                run.status = "failed"
                trace.write({"service": "run", "kind": "failed", "ok": False,
                             "error": run.error,
                             "traceback": scrub_secrets(traceback.format_exc())[-8000:]})
                run.emit("run_failed", {"error": run.error})


def data_source(settings, *, sandbox: bool) -> dict:
    """Where a run's work items come from - shown on the page and in the trace."""
    from urllib.parse import urlsplit

    if sandbox:
        return {"data_source": "simulated", "ewm_host": "", "jts_host": ""}
    return {"data_source": "live",
            "ewm_host": urlsplit(getattr(settings, "ewm_server", "") or "").hostname or "",
            "jts_host": urlsplit(getattr(settings, "jts_server", "") or "").hostname or ""}


# ------------------------------------------------------------------ the app

class Sessions:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self._sessions: dict[str, tuple[str, float]] = {}  # id -> (csrf, expires)

    def open(self) -> tuple[str, str]:
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._sessions[session_id] = (csrf, time.time() + SESSION_SECONDS)
        return session_id, csrf

    def csrf_for(self, session_id: str | None) -> str | None:
        found = self._sessions.get(session_id or "")
        if found is None or found[1] < time.time():
            self._sessions.pop(session_id or "", None)
            return None
        return found[0]


def create_app(manager: RunManager, *, port: int, access_token: str, environment: str,
               model: str, orchestration: str, source: dict | None = None):
    from fastapi import FastAPI, Request
    from fastapi.responses import (
        FileResponse,
        HTMLResponse,
        JSONResponse,
        RedirectResponse,
        StreamingResponse,
    )
    from pydantic import BaseModel, Field

    sessions = Sessions(access_token)
    allowed_hosts = {f"{LOOPBACK}:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions

    class RunBody(BaseModel):
        prompt: str = Field(max_length=MAX_PROMPT)
        mode: Literal["dry", "commit"] = "dry"
        confirm: str = Field(default="", max_length=20)

    class DecisionBody(BaseModel):
        approved: bool
        userids: list[str] = Field(default_factory=list, max_length=200)

    def refuse(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS rebinding: a hostile page may resolve its own name to 127.0.0.1.
        if request.headers.get("host", "") not in allowed_hosts:
            response = refuse(400, "unexpected Host header")
        elif request.method not in ("GET", "HEAD") and (
                request.headers.get("origin", "") not in allowed_origins):
            response = refuse(403, "request did not come from this console")
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    def session_of(request: Request) -> tuple[str, str] | None:
        session_id = request.cookies.get("alm_session")
        csrf = sessions.csrf_for(session_id)
        return (session_id, csrf) if csrf else None

    def require(request: Request, *, write: bool = False):
        found = session_of(request)
        if found is None:
            return refuse(401, "open the link printed in the terminal to sign in")
        if write and not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), found[1]):
            return refuse(403, "missing or wrong CSRF token")
        return None

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, token: str = ""):
        if token:
            if not hmac.compare_digest(token, access_token):
                return HTMLResponse((STATIC_DIR / "locked.html").read_text(encoding="utf-8"),
                                    status_code=401)
            session_id, _csrf = sessions.open()
            response = RedirectResponse("/", status_code=303)  # drop the token from history
            response.set_cookie("alm_session", session_id, httponly=True, samesite="strict",
                                max_age=SESSION_SECONDS, path="/")
            return response
        if session_of(request) is None:
            return HTMLResponse((STATIC_DIR / "locked.html").read_text(encoding="utf-8"),
                                status_code=401)
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/static/{name}")
    async def static(name: str):
        if name not in {"app.js", "app.css"}:
            return refuse(404, "not found")
        media = "text/javascript" if name.endswith(".js") else "text/css"
        return FileResponse(STATIC_DIR / name, media_type=media)

    @app.get("/api/session")
    async def session(request: Request):
        if (denied := require(request)) is not None:
            return denied
        return {"csrf": session_of(request)[1], "environment": environment,
                "sandbox": manager.sandbox, "model": model, "orchestration": orchestration,
                "operator": manager.operator, "max_commit_work_items": MAX_COMMIT_WORK_ITEMS,
                "confirm_word": "PROD" if environment == "PROD" else "COMMIT",
                **(source or {"data_source": "simulated" if manager.sandbox else "live",
                              "ewm_host": "", "jts_host": ""})}

    @app.get("/api/runs")
    async def list_runs(request: Request):
        if (denied := require(request)) is not None:
            return denied
        runs = sorted(manager.runs.values(), key=lambda r: r.created, reverse=True)
        return {"runs": [{k: v for k, v in r.summary().items() if k not in ("report",)}
                         for r in runs]}

    @app.post("/api/runs")
    async def start_run(request: Request, body: RunBody):
        if (denied := require(request, write=True)) is not None:
            return denied
        try:
            run = manager.start(body.prompt, body.mode, body.confirm)
        except RequestRefused as err:
            return refuse(422, str(err))
        except Exception as err:  # noqa: BLE001 - settings/model problems, shown plainly
            from alm_core.logging import scrub_secrets

            return refuse(500, scrub_secrets(str(err))[:400])
        return JSONResponse(run.summary(), status_code=201)

    @app.get("/api/runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        if (denied := require(request)) is not None:
            return denied
        run = manager.runs.get(run_id)
        return run.summary() if run else refuse(404, "no such run")

    @app.post("/api/runs/{run_id}/decision")
    async def decision(request: Request, run_id: str, body: DecisionBody):
        if (denied := require(request, write=True)) is not None:
            return denied
        try:
            manager.decide(run_id, body.approved, body.userids)
        except KeyError:
            return refuse(404, "no such run")
        except RequestRefused as err:
            return refuse(409, str(err))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(request: Request, run_id: str):
        if (denied := require(request, write=True)) is not None:
            return denied
        try:
            run = manager.stop(run_id)
        except KeyError:
            return refuse(404, "no such run")
        except RequestRefused as err:
            return refuse(409, str(err))
        return {"ok": True, "status": run.status}

    @app.get("/api/runs/{run_id}/trace")
    async def run_trace(request: Request, run_id: str, after: int = -1,
                        service: str = ""):
        if (denied := require(request)) is not None:
            return denied
        run = manager.runs.get(run_id)
        if run is None or run.trace is None:
            return refuse(404, "no such run")
        records = run.trace.since(after, limit=2000)
        last = records[-1]["seq"] if records else after
        if service:
            wanted = set(service.split(","))
            records = [r for r in records if r.get("service") in wanted]
        return {"records": records, "next": last, "path": str(run.trace.path),
                "done": run.status not in manager.ACTIVE}

    @app.get("/api/runs/{run_id}/trace.jsonl")
    async def run_trace_file(request: Request, run_id: str):
        if (denied := require(request)) is not None:
            return denied
        run = manager.runs.get(run_id)
        if run is None or run.trace is None or not Path(run.trace.path).is_file():
            return refuse(404, "no such run")
        return FileResponse(run.trace.path, media_type="application/x-ndjson",
                            filename=f"{run.thread_id}.jsonl")

    @app.get("/api/runs/{run_id}/events")
    async def events(request: Request, run_id: str, after: int = -1):
        if (denied := require(request)) is not None:
            return denied
        run = manager.runs.get(run_id)
        if run is None:
            return refuse(404, "no such run")

        async def stream():
            cursor, quiet = after, 0.0
            while True:
                with run.lock:
                    fresh = [e for e in run.events if e["seq"] > cursor]
                for event in fresh:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                if fresh:
                    quiet = 0.0
                elif run.status in ("done", "stopped", "failed"):
                    yield "event: end\ndata: {}\n\n"
                    return
                else:
                    quiet += 0.3
                    if quiet >= 15:
                        quiet = 0.0
                        yield ": keep-alive\n\n"
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.3)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    return app


# ------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent_web",
        description="The ALM agents' web console, on this machine only.")
    parser.add_argument("--sandbox", action="store_true",
                        help="use the simulated estate (no VPN, no Jazz password); the model is real")
    parser.add_argument("--port", type=int, default=8765, help="local port (default 8765)")
    parser.add_argument("--model", default="", help="override ALM_AGENT_MODEL")
    parser.add_argument("--rpm", type=float, default=0.0,
                        help="override ALM_LLM_REQUESTS_PER_MINUTE")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the link but do not open a browser")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from . import llm as llm_module
    from .sandbox import load_env

    load_env()
    from alm_core.logging import route_console

    # Everything goes to each run's trace; the terminal shows errors only.
    route_console("ERROR")
    if not 1024 <= args.port <= 65535:
        print("setup: --port must be between 1024 and 65535")
        return 2

    def llm_for(settings):
        agent_llm = llm_module.get_agent_llm(settings)
        if agent_llm is None:
            raise RuntimeError("no model client - run agent_local.py --check (or "
                               "agent_sandbox.py --check) to see why")
        return agent_llm, llm_module.get_supervisor_llm(settings)

    resolver = None
    if args.sandbox:
        from .sandbox import build_settings as sandbox_settings

        def settings_for(mode):
            return sandbox_settings(shadow=mode != "commit", model=args.model, rpm=args.rpm,
                                    orchestration="guided")
        probe = settings_for("dry")
    else:
        from alm_core.auth import JazzClient
        from alm_core.errors import AlmError

        from . import local

        def settings_for(mode):
            return local.build_settings(commit=mode == "commit", model=args.model,
                                        rpm=args.rpm)
        try:
            probe = settings_for("dry")
        except local.SetupError as err:
            print(f"setup: {err}")
            return 2
        # The password is typed here, once, and never goes through the browser.
        resolver = local.jazz_password_resolver(probe)
        local.pin_password(resolver, probe.password_secret_name,
                           resolver.get(probe.password_secret_name))
        client = JazzClient(probe, resolver)
        try:
            for kind, server in (("ewm", probe.ewm_server), ("jts", probe.jts_server)):
                client.session(server, kind=kind)
        except AlmError as err:
            print(f"setup: sign-in failed: {err.message}")
            return 2
        finally:
            client.close()

    access_token = secrets.token_urlsafe(32)
    manager = RunManager(sandbox=args.sandbox, settings_for=settings_for, llm_for=llm_for,
                         resolver=resolver)
    source = data_source(probe, sandbox=args.sandbox)
    app = create_app(manager, port=args.port, access_token=access_token,
                     environment=probe.environment, model=probe.agent_model,
                     orchestration=probe.orchestration, source=source)
    url = f"http://{LOOPBACK}:{args.port}/?token={access_token}"
    if args.sandbox:
        print("ALM agent console - SANDBOX: SIMULATED DATA. The work items and users are "
              "made up;\n  nothing reaches EWM, JTS or GPT. For real work items, start it "
              "without --sandbox.")
    else:
        print(f"ALM agent console - LIVE {probe.environment}: EWM {source['ewm_host']}, "
              f"JTS {source['jts_host']}, signed in as {probe.service_account}")
    print(f"Open this link in your browser (it signs you in; keep it private):\n  {url}")
    print("Stop the console with Ctrl+C.")
    if not args.no_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    import uvicorn

    uvicorn.run(app, host=LOOPBACK, port=args.port, log_level="warning",
                proxy_headers=False, server_header=False)
    # Ctrl+C here: a run in progress stops after its current step, not mid-write.
    if manager.active() is not None:
        print("Stopping the run in progress after its current step "
              "(a write in progress always finishes)...")
        stopped = manager.shutdown()
        if stopped is not None and stopped.status in RunManager.ACTIVE:
            print("The run did not end in time. Resume or re-run it later: the ledger "
                  "keeps anything already written from being written twice.")
    return 0
