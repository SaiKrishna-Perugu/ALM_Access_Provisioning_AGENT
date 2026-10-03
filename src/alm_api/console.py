"""The web console, served by the cloud API to signed-in people.

The same page as the laptop console (``alm_agents/web_static``), speaking the
same ``/api/*`` protocol, but answered from the shared store instead of one
process's memory - so any replica can serve any run, and a run's activity is
its trace, written by whichever worker drove it:

    GET  /                          the console (sign-in first)
    GET  /api/session               who you are, your roles, the CSRF token
    GET  /api/runs                  the run registry
    POST /api/runs                  start a run from plain words      (operator)
    GET  /api/runs/{id}             one run, its report, its card and votes
    POST /api/runs/{id}/decision    an approval vote                   (approver)
    POST /api/runs/{id}/stop        stop after the current step        (operator)
    GET  /api/runs/{id}/events      live activity (server-sent events)
    GET  /api/runs/{id}/trace       every call, cursor-paged (masked unless auditor)
    GET  /api/runs/{id}/trace.jsonl the trace as a download

Roles, CSRF (OIDC) and the same-origin check are the API's own
(``alm_api.main.require``); this module only translates.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

STATIC = Path(__file__).resolve().parents[1] / "alm_agents" / "web_static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
ACTIVE = ("queued", "running", "awaiting_approval", "stopping")
# Run registry status -> the status words the page knows.
STATUS = {"queued": "starting", "running": "running", "awaiting_approval": "awaiting_approval",
          "stopping": "stopping", "done": "done", "stopped": "stopped", "failed": "failed"}

router = APIRouter()


class StartBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="dry", pattern="^(dry|commit)$")
    confirm: str = Field(default="", max_length=20)


class DecisionBody(BaseModel):
    approved: bool
    userids: list[str] = Field(default_factory=list, max_length=200)


def _main():
    from . import main

    return main


def needs(role: str):
    """The API's own role check (and CSRF / same-origin), as a dependency."""

    def check(request: Request):
        return _main().require(role)(request)

    return check


def _epoch(stamp: str) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _page(name: str, status: int = 200) -> HTMLResponse:
    response = HTMLResponse((STATIC / name).read_text(encoding="utf-8"), status_code=status)
    response.headers.update({"Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})
    return response


# --------------------------------------------------------------- the page

@router.get("/", response_class=HTMLResponse)
async def page(request: Request):
    main = _main()
    if main.current_user(request) is None:
        if getattr(main.runtime.settings, "auth_mode", "iap") == "oidc":
            return RedirectResponse("/auth/login", status_code=303)
        return _page("denied.html", status=401)
    user = main.current_user(request)
    if not user.roles:
        return _page("denied.html", status=403)
    return _page("index.html")


@router.get("/static/{name}")
async def static(name: str):
    if name not in ("app.js", "app.css"):
        raise HTTPException(status_code=404)
    response = FileResponse(STATIC / name,
                            media_type="text/javascript" if name.endswith(".js") else "text/css")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------- session

@router.get("/api/session")
async def session(request: Request):
    from urllib.parse import urlsplit

    main = _main()
    user = main.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    settings = main.runtime.settings
    return {"csrf": user.csrf, "operator": user.identity, "roles": sorted(user.roles),
            "hosted": True, "auth_mode": getattr(settings, "auth_mode", "iap"),
            "environment": settings.environment, "sandbox": False,
            "writes": not settings.shadow_mode, "data_source": "live",
            "ewm_host": urlsplit(getattr(settings, "ewm_server", "") or "").hostname or "",
            "jts_host": urlsplit(getattr(settings, "jts_server", "") or "").hostname or "",
            "model": getattr(settings, "agent_model", ""),
            "orchestration": getattr(settings, "orchestration", ""),
            "max_commit_work_items": 5,
            "held": settings.held_operations() if hasattr(settings, "held_operations") else [],
            "confirm_word": "PROD" if settings.environment == "PROD" else "COMMIT"}


# ------------------------------------------------------------------- runs

def _summary(run: dict) -> dict:
    report = run.get("report") or None
    if report:
        report = {k: report.get(k) for k in ("halted", "halt_reason", "hops",
                                              "approval_rounds", "metrics", "version")} | {
            "results": [{k: r.get(k) for k in ("userid", "operation", "outcome",
                                                "work_item_id", "message", "replayed")}
                        for r in report.get("results") or []]}
    return {"id": run["thread_id"], "thread_id": run["thread_id"],
            "prompt": run.get("operator_request") or f"{run.get('trigger', 'run')}: "
                      f"{', '.join(run.get('scope') or []) or 'the active queue'}",
            "mode": run.get("mode") or "dry", "work_items": run.get("scope") or [],
            "sandbox": False, "status": STATUS.get(run.get("status"), run.get("status")),
            "created": _epoch(run.get("created_at", "")), "error": run.get("error", ""),
            "requested_by": run.get("requested_by", ""), "report": report,
            "stopped_by": "", "trace": ""}


@router.get("/api/runs")
async def runs(user=Depends(needs("viewer"))):
    main = _main()
    return {"runs": [_summary(r) for r in await main.runtime.store.list_runs(100)]}


async def _run_or_404(thread_id: str) -> dict:
    run = await _main().runtime.store.get_run(thread_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return run


@router.get("/api/runs/{thread_id}")
async def one_run(thread_id: str,
                  user=Depends(needs("viewer"))):
    main = _main()
    run = await _run_or_404(thread_id)
    summary = _summary(run)
    stop = await main.runtime.store.stop_request(thread_id)
    summary["stopped_by"] = stop["by"] if stop else ""
    if run.get("status") == "awaiting_approval":
        view = await main._approval_view(thread_id)
        summary["pending"] = {**view["card"], "needed": view["needed"],
                              "votes": view["votes"], "tally": view["tally"],
                              "can_vote": user.has("approver")}
    else:
        summary["pending"] = None
    return summary


@router.post("/api/runs", status_code=201)
async def start(body: StartBody,
                user=Depends(needs("operator"))):
    main = _main()
    answer = await main.start_run(main.StartPayload(prompt=body.prompt, mode=body.mode,
                                                    confirm=body.confirm), user=user)
    return _summary(await _run_or_404(answer["thread_id"]))


@router.post("/api/runs/{thread_id}/decision")
async def decision(thread_id: str, body: DecisionBody,
                   user=Depends(needs("approver"))):
    main = _main()
    view = await main.vote(thread_id, main.ApprovalPayload(
        approved=body.approved, approved_userids=body.userids), user=user)
    return {"ok": True, "tally": view["tally"]}


@router.post("/api/runs/{thread_id}/stop")
async def stop(thread_id: str,
               user=Depends(needs("operator"))):
    answer = await _main().stop_run(thread_id, None, user=user)
    return {"ok": True, "status": STATUS.get(answer["status"], answer["status"])}


# --------------------------------------------------------------- activity

def _event(record: dict, run: dict) -> tuple[str, dict] | None:
    """A trace record as the page's activity event, or None if it is not one."""
    service, kind = record.get("service"), record.get("kind")
    data = {k: v for k, v in record.items()
            if k not in ("seq", "at", "t", "thread_id", "service", "kind", "cursor")}
    if service == "supervisor":
        return "supervisor", data
    if service == "tool" and kind == "tool_call":
        return "tool_call", data
    if service == "agent" and kind == "agent_text":
        return "agent_text", data
    if service == "model" and kind == "model_error":
        return "model_error", data
    if service == "approval" and kind == "preview":
        return "approval_preview", {"items": [{"userid": u} for u in data.get("users", [])],
                                    "reason": data.get("reason", "")}
    if service == "run" and kind == "job_start" and int(data.get("attempt", 1)) == 1:
        from urllib.parse import urlsplit

        settings = _main().runtime.settings
        return "run_started", {
            "mode": run.get("mode"), "environment": run.get("environment"),
            "work_items": run.get("scope") or "the active queue",
            "data_source": "live", "sandbox": False,
            "ewm_host": urlsplit(getattr(settings, "ewm_server", "") or "").hostname or "",
            "jts_host": urlsplit(getattr(settings, "jts_server", "") or "").hostname or ""}
    if service == "run" and kind == "parked":
        return "approval_required", {"reason": "waiting for approval", "items": []}
    if service == "run" and kind == "job_resume":
        return "approval_decided", {"approved": True, "approver": "the approvers",
                                    "userids": []}
    if service == "run" and kind == "stopped":
        return "stopped", data
    if service == "run" and kind == "finished":
        return "run_finished", {"halted": data.get("halted"),
                                "halt_reason": data.get("halt_reason", ""),
                                "stopped": data.get("status") == "stopped",
                                "metrics": data.get("metrics") or {}}
    return None


async def _card(thread_id: str, user) -> dict:
    try:
        view = await _main()._approval_view(thread_id)
    except HTTPException:
        return {"reason": "waiting for approval", "items": []}
    return {**view["card"], "needed": view["needed"], "votes": view["votes"],
            "tally": view["tally"], "can_vote": user.has("approver")}


async def _decided(thread_id: str) -> dict:
    _request, decision = await _main().runtime.store.get_approval(thread_id)
    if decision is None:
        return {"approved": True, "approver": "the approvers", "userids": []}
    return {"approved": decision.approved, "approver": decision.approver,
            "userids": list(decision.approved_userids)}


@router.get("/api/runs/{thread_id}/events")
async def events(thread_id: str, request: Request, after: int = 0,
                 user=Depends(needs("viewer"))):
    main = _main()
    await _run_or_404(thread_id)
    store = main.runtime.store

    async def stream():
        cursor, quiet = after, 0.0
        while True:
            run = await store.get_run(thread_id) or {}
            records = await store.trace_since(thread_id, after=cursor, limit=500)
            for record in records:
                cursor = record["cursor"]
                found = _event(record, run)
                if found is None:
                    continue
                kind, data = found
                if kind == "approval_required":
                    data = await _card(thread_id, user)
                elif kind == "approval_decided":
                    data = await _decided(thread_id)
                if not user.has("auditor"):
                    # Requesters' data is masked; who voted is not - approvers
                    # must see who else has decided.
                    keep = {k: data[k] for k in ("votes", "tally", "approver") if k in data}
                    data = {**main._masked(data), **keep}
                yield (f"id: {cursor}\ndata: " + json.dumps(
                    {"seq": cursor, "kind": kind, "at": record.get("at"), "data": data},
                    default=str) + "\n\n")
            if records:
                quiet = 0.0
                continue
            if run.get("status") in ("done", "stopped", "failed"):
                if run.get("status") == "failed":
                    yield ("data: " + json.dumps({"seq": cursor + 1, "kind": "run_failed",
                                                  "data": {"error": run.get("error", "")}})
                           + "\n\n")
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            quiet += 1.0
            if quiet >= 15:
                quiet = 0.0
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/api/runs/{thread_id}/trace")
async def trace(thread_id: str, after: int = 0,
                user=Depends(needs("viewer"))):
    main = _main()
    run = await _run_or_404(thread_id)
    records = await main.runtime.store.trace_since(thread_id, after=max(after, 0), limit=2000)
    following = records[-1]["cursor"] if records else after
    if not user.has("auditor"):
        records = [main._masked(r) for r in records]
    return {"records": records, "next": following, "path": "the shared store",
            "done": run.get("status") not in ACTIVE}


@router.get("/api/runs/{thread_id}/trace.jsonl")
async def trace_file(thread_id: str,
                     user=Depends(needs("viewer"))):
    main = _main()
    await _run_or_404(thread_id)
    records = await main.runtime.store.trace_since(thread_id, after=0, limit=100_000)
    if not user.has("auditor"):
        records = [main._masked(r) for r in records]
    body = "".join(json.dumps(r, default=str) + "\n" for r in records)
    return StreamingResponse(
        iter([body]), media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{thread_id}.jsonl"'})
