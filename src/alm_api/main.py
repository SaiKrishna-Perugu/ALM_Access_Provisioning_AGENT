"""FastAPI surface: triggers in, decisions in, runs out.

Endpoints:

    POST /webhooks/ewm              HMAC-authenticated trigger for one work item
    POST /approvals/{thread}        a human's decision; queues the run's resume
    GET  /approvals/{thread}        the fallback approval page
    GET  /runs                      the run registry, newest first
    GET  /runs/{thread}             one run: registry row, approval, audit trail
    POST /runs/{thread}/stop        stop a run after its current step
    GET  /runs/{thread}/trace       the run's trace records (cursor-paged)
    POST /runs                      start a run (operator)
    GET  /queue                     job counts by status, and dead jobs
    POST /admin/reconcile           queue a reconciliation sweep now (admin)
    GET  /auth/login /auth/callback /auth/logout   OIDC sign-in (ALM_AUTH_MODE=oidc)
    GET  /me                        who you are and what you may do
    GET  /healthz /readyz           liveness and readiness

Everything except the webhook (HMAC), the health checks and sign-in needs a
signed-in person with the right role - see ``alm_api.auth``.

Nothing here runs a run. Every trigger becomes a job in the store's queue,
and workers - embedded in this process (``ALM_WORKER_CONCURRENCY``, default 1)
or separate (``python -m alm_agents.worker``) - claim and drive them. So any
number of API replicas can sit behind a load balancer: none holds run state,
and a deploy or a crash loses nothing that a worker will not pick up again.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from alm_core.credentials import build_resolver
from alm_core.logging import configure, get_logger
from alm_core.models import ApprovalDecision

from . import auth
from .security import caller_identity, verify_approval_token, verify_webhook

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from pydantic import BaseModel, Field
except ImportError as err:  # pragma: no cover
    raise ImportError("alm_api needs the cloud extras: "
                      "pip install -r requirements-cloud.txt") from err

log = get_logger("alm.api")


class ApprovalPayload(BaseModel):
    approved: bool
    token: str = ""
    comment: str = ""
    approved_userids: list[str] = Field(default_factory=list)


class WebhookPayload(BaseModel):
    """What the EWM bridge posts. Only the work item id is trusted."""

    work_item_id: str = Field(min_length=1, max_length=32, pattern=r"^\d{1,10}$")
    event: str = "modified"
    state: str = ""


class StopPayload(BaseModel):
    reason: str = Field(default="", max_length=200)


class StartPayload(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    mode: str = Field(default="dry", pattern="^(dry|commit)$")
    confirm: str = Field(default="", max_length=20)


class Runtime:
    """The process's shared services, its embedded workers, and its store."""

    def __init__(self):
        self.services = None
        self.settings = None
        self.resolver = None
        self.stack: contextlib.AsyncExitStack | None = None
        self.worker_task: asyncio.Task | None = None
        self.stopping: asyncio.Event | None = None

    @property
    def store(self):
        return self.services.store

    def secret(self, name: str) -> str:
        try:
            return self.resolver.get(name)
        except Exception as err:  # noqa: BLE001 - a missing secret is a 503, not a crash
            log.warning("secret_unavailable", secret=name, error=str(err))
            return ""


runtime = Runtime()


def thread_for(work_item_id: str) -> str:
    """One durable thread per work item.

    Using the work item as the thread id means a redelivered webhook finds the
    run already queued instead of starting a rival one, and an approver who
    comes back an hour later finds the same conversation.
    """
    return f"wi-{work_item_id}"


def _mode() -> str:
    return "dry" if runtime.settings.shadow_mode else "commit"


# --------------------------------------------------------------- identity

def current_user(request: Request) -> auth.User | None:
    """The signed-in person, or None. Never trusts a header the proxy did not set."""
    settings = runtime.settings
    if getattr(settings, "auth_mode", "iap") == "oidc":
        secret = runtime.secret(settings.session_secret_name)
        if not secret:
            return None
        return auth.user_from_session(secret, request.cookies.get(auth.SESSION_COOKIE, ""))
    identity = caller_identity(request.headers)
    if identity == "unknown":
        return None
    return auth.User(subject=identity, email=identity,
                     roles=auth.roles_for(email=identity,
                                          role_map=getattr(settings, "role_map", "{}")))


def require(role: str):
    """A dependency: a signed-in person holding ``role``. In OIDC mode a request
    that changes something must also carry the session's CSRF token."""

    def check(request: Request) -> auth.User:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in first")
        if not user.has(role):
            raise HTTPException(status_code=403, detail=f"this needs the {role} role")
        if (request.method not in ("GET", "HEAD") and
                getattr(runtime.settings, "auth_mode", "iap") == "oidc" and
                not secrets_equal(request.headers.get("x-csrf-token", ""), user.csrf)):
            raise HTTPException(status_code=403, detail="missing or wrong CSRF token")
        return user

    return check


def secrets_equal(a: str, b: str) -> bool:
    import hmac

    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def _masked(record: dict) -> dict:
    """A trace record with e-mail addresses removed, for people without the
    auditor role. User IDs stay: they are what an operator works with."""
    import json

    from alm_core.logging import redact_pii

    return json.loads(redact_pii(json.dumps(record, default=str), keep_userids=True))


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    from alm_agents.graph import build_services
    from alm_agents.worker import Worker
    from alm_core.config import get_settings

    from .notify import make_notifier

    configure()
    settings = get_settings()
    runtime.settings = settings
    runtime.resolver = build_resolver(settings)

    stack = contextlib.AsyncExitStack()
    runtime.services = await stack.enter_async_context(
        build_services(settings, notifier=make_notifier(settings, runtime.resolver)))
    runtime.stack = stack

    if settings.worker_concurrency > 0:
        runtime.stopping = asyncio.Event()
        runtime.worker_task = asyncio.create_task(
            Worker(runtime.services).run_forever(runtime.stopping))

    log.info("api_started", environment=settings.environment, shadow=settings.shadow_mode,
             embedded_workers=settings.worker_concurrency)
    try:
        yield
    finally:
        if runtime.worker_task is not None:
            runtime.stopping.set()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.worker_task
        await stack.aclose()
        log.info("api_stopped")


app = FastAPI(title="ALM Access Provisioning", version="3.0.0", lifespan=lifespan)


# ------------------------------------------------------------------- health

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    ready = runtime.services is not None
    if ready:
        try:
            await runtime.store.queue_depth()
        except Exception:  # noqa: BLE001 - the database is unreachable
            ready = False
    return JSONResponse({"ready": ready,
                         "environment": getattr(runtime.settings, "environment", ""),
                         "shadow_mode": getattr(runtime.settings, "shadow_mode", None)},
                        status_code=200 if ready else 503)


# ------------------------------------------------------------------ webhook

@app.post("/webhooks/ewm", status_code=202)
async def ewm_webhook(payload: WebhookPayload, request: Request,
                      x_alm_signature: str = Header(default=""),
                      x_alm_timestamp: str = Header(default=""),
                      x_alm_delivery: str = Header(default="")) -> dict:
    """Trigger a run for one work item. Authenticated, replay-protected."""
    from alm_agents.worker import submit_run

    body = await request.body()
    secret = runtime.secret(runtime.settings.webhook_secret_name)
    ok, reason = verify_webhook(secret, body=body, signature=x_alm_signature,
                                timestamp=x_alm_timestamp)
    if ok and x_alm_delivery and not await runtime.store.remember_delivery(x_alm_delivery):
        ok, reason = False, f"delivery {x_alm_delivery} has already been processed"
    if not ok:
        # Deliberately terse: a caller who cannot authenticate learns nothing
        # about why beyond the log line we keep.
        log.warning("webhook_rejected", reason=reason, delivery=x_alm_delivery)
        raise HTTPException(status_code=401, detail="unauthenticated webhook")

    thread_id = thread_for(payload.work_item_id)
    job = await submit_run(runtime.store, thread_id=thread_id,
                           work_item_ids=[payload.work_item_id], mode=_mode(),
                           requested_by="webhook:ewm", trigger="webhook",
                           environment=runtime.settings.environment)
    log.info("webhook_accepted", work_item=payload.work_item_id, thread_id=thread_id,
             queued=job is not None)
    return {"accepted": True, "thread_id": thread_id, "queued": job is not None}


# ----------------------------------------------------------------- approvals

async def _load_approval(thread_id: str):
    request, decision = await runtime.store.get_approval(thread_id)
    if request is None:
        raise HTTPException(status_code=404, detail="no approval for that thread")
    return request, decision


@app.get("/approvals/{thread_id}", response_class=HTMLResponse)
async def approval_page(thread_id: str, request: Request, token: str = "",
                        decision: str = "") -> Any:
    """The browser fallback for approving, and the target of the card's buttons."""
    from html import escape

    approval_request, existing = await _load_approval(thread_id)
    secret = runtime.secret(runtime.settings.approval_signing_secret_name)
    ok, reason, _claims = verify_approval_token(
        secret, token, thread_id=thread_id, plan_hash=approval_request.plan_hash)
    if not ok:
        return HTMLResponse(f"<h1>Link no longer valid</h1><p>{escape(reason)}.</p>"
                            "<p>Ask for a fresh approval card.</p>", status_code=403)

    if decision in ("approve", "reject"):
        if existing is not None:
            raise HTTPException(status_code=409,
                                detail=f"already decided by {existing.approver}")
        recorded = await _record_decision(
            thread_id, approved=decision == "approve",
            approver=caller_identity(request.headers),
            plan_hash=approval_request.plan_hash, comment="via approval link")
        return HTMLResponse(
            f"<h1>{'Approved' if recorded.approved else 'Rejected'}</h1>"
            f"<p>{approval_request.user_count} user(s) on "
            f"{approval_request.work_item_count} work item(s).</p>"
            f"<p>Recorded against {escape(recorded.approver)}.</p>")

    if existing is not None:
        return HTMLResponse(
            f"<h1>Already decided</h1><p>{'Approved' if existing.approved else 'Rejected'} "
            f"by {escape(existing.approver)} at {existing.decided_at:%Y-%m-%d %H:%M} UTC.</p>")

    rows = "".join(
        f"<tr><td>{escape(i.userid)}</td><td>{escape(i.display_name)}</td>"
        f"<td>{escape(i.action)}</td><td>{escape(i.risk.value)}</td>"
        f"<td>{escape('; '.join(i.risk_reasons))}</td></tr>"
        for i in approval_request.items)
    safe_token = escape(token, quote=True)
    return HTMLResponse(f"""
<h1>ALM access provisioning - {escape(approval_request.environment)}</h1>
<p>{approval_request.user_count} user(s), {approval_request.work_item_count} work item(s).
   Expires {approval_request.expires_at:%Y-%m-%d %H:%M} UTC.</p>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>User</th><th>Name</th><th>Action</th><th>Risk</th><th>Flags</th></tr>{rows}
</table>
<p>
  <a href="?decision=approve&token={safe_token}">Approve all</a> |
  <a href="?decision=reject&token={safe_token}">Reject</a>
</p>""")


@app.post("/approvals/{thread_id}")
async def submit_approval(thread_id: str, payload: ApprovalPayload,
                          request: Request) -> dict:
    """Record a decision and queue the parked run's resume."""
    approval_request, existing = await _load_approval(thread_id)
    if existing is not None:
        raise HTTPException(status_code=409,
                            detail=f"already decided by {existing.approver}")

    secret = runtime.secret(runtime.settings.approval_signing_secret_name)
    identity = caller_identity(request.headers)
    if payload.token:
        ok, reason, _ = verify_approval_token(
            secret, payload.token, thread_id=thread_id,
            plan_hash=approval_request.plan_hash)
        if not ok:
            raise HTTPException(status_code=403, detail=reason)
    elif identity == "unknown":
        # Neither a signed token nor an authenticated identity: there would be
        # nobody to name in the audit row, so there is no approval to record.
        raise HTTPException(status_code=401,
                            detail="an approval token or an authenticated caller is required")

    decision = await _record_decision(
        thread_id, approved=payload.approved, approver=identity,
        plan_hash=approval_request.plan_hash, comment=payload.comment,
        approved_userids=payload.approved_userids)
    return {"recorded": True, "approved": decision.approved,
            "approver": decision.approver, "thread_id": thread_id}


async def _record_decision(thread_id: str, *, approved: bool, approver: str,
                           plan_hash: str, comment: str = "",
                           approved_userids: list[str] | None = None) -> ApprovalDecision:
    from alm_agents.worker import submit_decision

    decision = ApprovalDecision(thread_id=thread_id, approved=approved,
                                approver=approver, plan_hash=plan_hash,
                                comment=comment,
                                approved_userids=approved_userids or [])
    await runtime.store.save_approval_decision(decision)
    log.info("approval_recorded", thread_id=thread_id, approved=approved,
             approver=approver)
    # The resume is a job: whichever worker claims it continues the run.
    await submit_decision(runtime.store, thread_id, decision)
    return decision


# ---------------------------------------------------------------------- runs

@app.get("/runs")
async def list_runs(limit: int = 50, _user=Depends(require("viewer"))) -> dict:
    return {"runs": await runtime.store.list_runs(min(max(limit, 1), 200))}


@app.get("/runs/{thread_id}")
async def get_run(thread_id: str, user=Depends(require("viewer"))) -> dict:
    run = await runtime.store.get_run(thread_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    approval_request, decision = await runtime.store.get_approval(thread_id)
    events = await runtime.store.run_events(run["run_id"]) if run["run_id"] else []
    if not user.has("auditor"):
        events = [_masked(e) for e in events]
    return {**run,
            "approval": approval_request.model_dump(mode="json") if approval_request else None,
            "decision": decision.model_dump(mode="json") if decision else None,
            "events": events}


@app.post("/runs/{thread_id}/stop", status_code=202)
async def stop_run(thread_id: str, payload: StopPayload | None = None,
                   user=Depends(require("operator"))) -> dict:
    """Stop a run after its current step. A write in progress always finishes."""
    from alm_agents.worker import request_stop

    identity = user.identity
    try:
        status = await request_stop(runtime.store, thread_id, identity)
    except LookupError:
        raise HTTPException(status_code=404, detail="unknown run") from None
    log.info("stop_requested", thread_id=thread_id, by=identity,
             reason=(payload.reason if payload else ""))
    return {"thread_id": thread_id, "status": status}


@app.get("/runs/{thread_id}/trace")
async def run_trace(thread_id: str, after: int = 0, limit: int = 500,
                    user=Depends(require("viewer"))) -> dict:
    if await runtime.store.get_run(thread_id) is None:
        raise HTTPException(status_code=404, detail="unknown run")
    records = await runtime.store.trace_since(thread_id, after=max(after, 0),
                                              limit=min(max(limit, 1), 2000))
    following = records[-1]["cursor"] if records else after
    if not user.has("auditor"):
        records = [_masked(r) for r in records]
    return {"records": records, "next": following}


@app.post("/runs", status_code=201)
async def start_run(payload: StartPayload, user=Depends(require("operator"))) -> dict:
    """Start a run from a request in plain words. Work items are the numbers in
    it; writing needs 1-5 of them and the confirmation word. The words never
    choose the mode or widen the scope."""
    import uuid

    from alm_agents.web import RequestRefused, parse_request
    from alm_agents.worker import submit_run

    settings = runtime.settings
    if payload.mode == "commit" and settings.shadow_mode:
        raise HTTPException(status_code=422, detail="this deployment does not write")
    try:
        work_items = parse_request(payload.prompt, payload.mode, payload.confirm,
                                   settings.environment)
    except RequestRefused as err:
        raise HTTPException(status_code=422, detail=str(err)) from None
    thread_id = (thread_for(work_items[0]) if len(work_items) == 1
                 else f"console-{uuid.uuid4().hex[:10]}")
    job = await submit_run(runtime.store, thread_id=thread_id, work_item_ids=work_items,
                           mode=payload.mode, requested_by=user.identity, trigger="console",
                           operator_request=payload.prompt.strip(),
                           environment=settings.environment)
    if job is None:
        raise HTTPException(status_code=409, detail=f"{thread_id} is already queued or running")
    return {"thread_id": thread_id, "work_items": work_items, "mode": payload.mode}


@app.get("/queue")
async def queue(_user=Depends(require("viewer"))) -> dict:
    return {"depth": await runtime.store.queue_depth(),
            "dead": await runtime.store.list_jobs(status="dead", limit=50)}


@app.post("/admin/reconcile", status_code=202)
async def force_reconcile(user=Depends(require("admin"))) -> dict:
    """Queue a sweep of the active queue now, without waiting for the schedule."""
    from alm_agents.worker import submit_run

    identity = user.identity
    thread_id = f"reconcile-manual-{int(time.time())}"
    await submit_run(runtime.store, thread_id=thread_id, work_item_ids=None, mode=_mode(),
                     requested_by=identity, trigger="reconcile",
                     environment=runtime.settings.environment)
    return {"accepted": True, "thread_id": thread_id}


# ------------------------------------------------------------------ sign-in

def _base_url(request: Request) -> str:
    return (runtime.settings.approval_base_url or str(request.base_url)).rstrip("/")


def _oidc() -> auth.Oidc:
    settings = runtime.settings
    if getattr(settings, "auth_mode", "iap") != "oidc":
        raise HTTPException(status_code=404, detail="sign-in is handled by the proxy")
    client_secret = runtime.secret(settings.oidc_client_secret_name)
    if not (settings.oidc_issuer and settings.oidc_client_id and client_secret and
            runtime.secret(settings.session_secret_name)):
        raise HTTPException(status_code=503, detail="OIDC sign-in is not configured")
    return auth.Oidc(settings, client_secret)


@app.get("/auth/login")
async def login(request: Request) -> RedirectResponse:
    oidc = _oidc()
    url, pending = oidc.start(f"{_base_url(request)}/auth/callback")
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(auth.LOGIN_COOKIE,
                        auth.sign(runtime.secret(runtime.settings.session_secret_name),
                                  pending),
                        max_age=auth.LOGIN_SECONDS, httponly=True, secure=True,
                        samesite="lax", path="/auth")
    return response


@app.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    oidc = _oidc()
    secret = runtime.secret(runtime.settings.session_secret_name)
    pending = auth.unsign(secret, request.cookies.get(auth.LOGIN_COOKIE, ""))
    if pending is None or not state or not secrets_equal(state, pending.get("state", "")):
        raise HTTPException(status_code=400, detail="sign-in expired or was not started here")
    try:
        claims = oidc.finish(code=code, redirect_uri=f"{_base_url(request)}/auth/callback",
                             login=pending)
    except PermissionError as err:
        log.warning("sign_in_refused", reason=str(err))
        raise HTTPException(status_code=401, detail=str(err)) from None
    user = oidc.user(claims)
    if not user.roles:
        log.warning("sign_in_without_role", identity=user.identity)
        raise HTTPException(status_code=403, detail=f"{user.identity} has no role here")
    log.info("signed_in", identity=user.identity, roles=sorted(user.roles))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.LOGIN_COOKIE, path="/auth")
    response.set_cookie(auth.SESSION_COOKIE,
                        auth.session_for(secret, user, runtime.settings.session_hours),
                        max_age=int(runtime.settings.session_hours * 3600), httponly=True,
                        secure=True, samesite="strict", path="/")
    return response


@app.get("/auth/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/me")
async def me(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in first")
    return {**user.public(), "csrf": user.csrf,
            "environment": runtime.settings.environment,
            "writes": not runtime.settings.shadow_mode}
