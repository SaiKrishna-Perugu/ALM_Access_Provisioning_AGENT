"""FastAPI surface: webhook in, approval in, runs out.

Endpoints:

    POST /webhooks/ewm          HMAC-authenticated trigger for one work item
    POST /approvals/{thread}    a human's decision; resumes the parked run
    GET  /approvals/{thread}    the fallback approval page (IAP-authenticated)
    GET  /runs                  recent runs
    GET  /runs/{run_id}         the audit trail of one run
    POST /admin/reconcile       force a reconciliation sweep now
    GET  /healthz /readyz       liveness and readiness

Runs execute as background tasks, not inside the request. A provisioning run
waits on a 30-minute permission poll and on a human approval; holding an HTTP
connection open for either would be absurd, and the webhook sender would retry
into a duplicate trigger.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from alm_core.credentials import build_resolver
from alm_core.logging import configure, get_logger
from alm_core.models import ApprovalDecision

from .chat import build_card, post_card
from .security import (
    ReplayGuard,
    caller_identity,
    issue_approval_token,
    verify_approval_token,
    verify_webhook,
)

try:
    from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse
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

    work_item_id: str = Field(min_length=1, max_length=32)
    event: str = "modified"
    state: str = ""


class Runtime:
    """The process's shared services and background state.

    ``services`` are shared by every run; each start and each resume gets its
    own graph and tool context from ``run_session``. ``ctx`` is a context with
    no run of its own, for the handlers that only read or write the store.
    """

    def __init__(self):
        self.services = None
        self.ctx = None
        self.settings = None
        self.resolver = None
        self.replay_guard = ReplayGuard()
        self.stack: contextlib.AsyncExitStack | None = None
        self.reconcile_task: asyncio.Task | None = None
        # One run at a time per thread; the lock stops a redelivered webhook
        # from starting a second run over the same checkpoint.
        self.locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, thread_id: str) -> asyncio.Lock:
        return self.locks.setdefault(thread_id, asyncio.Lock())

    def secret(self, name: str) -> str:
        try:
            return self.resolver.get(name)
        except Exception as err:  # noqa: BLE001 - a missing secret is a 503, not a crash
            log.warning("secret_unavailable", secret=name, error=str(err))
            return ""


runtime = Runtime()


def thread_for(work_item_id: str) -> str:
    """One durable thread per work item.

    Using the work item as the thread id means a redelivered webhook resumes the
    existing run instead of starting a rival one, and an approver who comes back
    an hour later finds the same conversation.
    """
    return f"wi-{work_item_id}"


async def _notifier(request) -> None:
    """Deliver the approval card for a parked run."""
    settings = runtime.settings
    secret = runtime.secret(settings.approval_signing_secret_name)
    if not secret:
        log.warning("approval_token_unavailable", thread_id=request.thread_id)
        return
    token = issue_approval_token(secret, thread_id=request.thread_id,
                                 plan_hash=request.plan_hash,
                                 expires_at=request.expires_at.timestamp())
    base = settings.approval_base_url.rstrip("/")
    card = build_card(
        request,
        approve_url=f"{base}/approvals/{request.thread_id}?decision=approve&token={token}",
        reject_url=f"{base}/approvals/{request.thread_id}?decision=reject&token={token}",
        review_url=f"{base}/approvals/{request.thread_id}?token={token}")
    post_card(settings.chat_webhook_url, card)


async def _reconcile_loop() -> None:
    """The safety net for a trigger mechanism that cannot be relied on.

    EWM/RTC has no first-class outbound webhook, so a missed event is expected,
    not exceptional. This sweeps the whole active queue on a timer; the
    idempotency ledger makes the overlap with webhook-triggered runs harmless.
    """
    settings = runtime.settings
    interval = settings.reconcile_interval_minutes * 60
    while True:
        try:
            await asyncio.sleep(interval)
            log.info("reconcile_sweep_starting")
            await _run_in_background(thread_id=f"reconcile-{int(time.time())}",
                                     work_item_ids=None, trigger="reconcile")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must outlive one bad sweep
            log.exception("reconcile_sweep_failed")


async def _run_in_background(*, thread_id: str, work_item_ids: list[str] | None,
                             trigger: str) -> None:
    from alm_agents.graph import run_session, start_run

    async with runtime.lock_for(thread_id):
        try:
            # A graph and context of its own: a run must never share its board,
            # budgets or approval with another run in this process.
            graph, ctx = run_session(runtime.services)
            result = await start_run(graph, ctx, thread_id=thread_id,
                                     work_item_ids=work_item_ids, trigger=trigger)
        except Exception:  # noqa: BLE001 - one run must not take the service down
            log.exception("run_failed", thread_id=thread_id)
            return
        if "__interrupt__" in (result or {}):
            log.info("run_parked_for_approval", thread_id=thread_id)


async def _resume_in_background(thread_id: str, decision: ApprovalDecision) -> None:
    from alm_agents.graph import resume_run, run_session

    async with runtime.lock_for(thread_id):
        try:
            graph, ctx = run_session(runtime.services)
            await resume_run(graph, ctx, thread_id=thread_id, decision=decision)
        except Exception:  # noqa: BLE001
            log.exception("resume_failed", thread_id=thread_id)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    from alm_agents.graph import build_services
    from alm_core.config import get_settings
    from alm_core.tools.base import ToolContext

    configure()
    settings = get_settings()
    runtime.settings = settings
    runtime.resolver = build_resolver(settings)

    stack = contextlib.AsyncExitStack()
    services = await stack.enter_async_context(build_services(settings, notifier=_notifier))
    runtime.services, runtime.stack = services, stack
    runtime.ctx = ToolContext(settings=settings, client=services.client,
                              store=services.store, run_id="")

    if settings.reconcile_interval_minutes > 0:
        runtime.reconcile_task = asyncio.create_task(_reconcile_loop())

    log.info("api_started", environment=settings.environment,
             shadow=settings.shadow_mode,
             reconcile_minutes=settings.reconcile_interval_minutes)
    try:
        yield
    finally:
        if runtime.reconcile_task:
            runtime.reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.reconcile_task
        await stack.aclose()
        log.info("api_stopped")


app = FastAPI(title="ALM Access Provisioning", version="2.0.0", lifespan=lifespan)


# ------------------------------------------------------------------- health

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    ready = runtime.services is not None and runtime.ctx is not None
    return JSONResponse({"ready": ready,
                         "environment": getattr(runtime.settings, "environment", ""),
                         "shadow_mode": getattr(runtime.settings, "shadow_mode", None)},
                        status_code=200 if ready else 503)


# ------------------------------------------------------------------ webhook

@app.post("/webhooks/ewm", status_code=202)
async def ewm_webhook(payload: WebhookPayload, request: Request,
                      background: BackgroundTasks,
                      x_alm_signature: str = Header(default=""),
                      x_alm_timestamp: str = Header(default=""),
                      x_alm_delivery: str = Header(default="")) -> dict:
    """Trigger a run for one work item. Authenticated, replay-protected."""
    body = await request.body()
    secret = runtime.secret(runtime.settings.webhook_secret_name)
    ok, reason = verify_webhook(secret, body=body, signature=x_alm_signature,
                                timestamp=x_alm_timestamp, delivery_id=x_alm_delivery,
                                guard=runtime.replay_guard)
    if not ok:
        # Deliberately terse: a caller who cannot authenticate learns nothing
        # about why beyond the log line we keep.
        log.warning("webhook_rejected", reason=reason, delivery=x_alm_delivery)
        raise HTTPException(status_code=401, detail="unauthenticated webhook")

    thread_id = thread_for(payload.work_item_id)
    background.add_task(_run_in_background, thread_id=thread_id,
                        work_item_ids=[payload.work_item_id], trigger="webhook")
    log.info("webhook_accepted", work_item=payload.work_item_id, thread_id=thread_id)
    return {"accepted": True, "thread_id": thread_id}


# ----------------------------------------------------------------- approvals

async def _load_approval(thread_id: str):
    request, decision = await runtime.ctx.store.get_approval(thread_id)
    if request is None:
        raise HTTPException(status_code=404, detail="no approval for that thread")
    return request, decision


@app.get("/approvals/{thread_id}", response_class=HTMLResponse)
async def approval_page(thread_id: str, request: Request, token: str = "",
                        decision: str = "") -> Any:
    """The browser fallback for approving, and the target of the card's buttons."""
    approval_request, existing = await _load_approval(thread_id)
    secret = runtime.secret(runtime.settings.approval_signing_secret_name)
    ok, reason, _claims = verify_approval_token(
        secret, token, thread_id=thread_id, plan_hash=approval_request.plan_hash)
    if not ok:
        return HTMLResponse(f"<h1>Link no longer valid</h1><p>{reason}.</p>"
                            "<p>Ask for a fresh approval card.</p>", status_code=403)

    if decision in ("approve", "reject"):
        recorded = await _record_decision(
            thread_id, approved=decision == "approve",
            approver=caller_identity(request.headers),
            plan_hash=approval_request.plan_hash, comment="via approval link")
        return HTMLResponse(
            f"<h1>{'Approved' if recorded.approved else 'Rejected'}</h1>"
            f"<p>{approval_request.user_count} user(s) on "
            f"{approval_request.work_item_count} work item(s).</p>"
            f"<p>Recorded against {recorded.approver}.</p>")

    if existing is not None:
        return HTMLResponse(
            f"<h1>Already decided</h1><p>{'Approved' if existing.approved else 'Rejected'} "
            f"by {existing.approver} at {existing.decided_at:%Y-%m-%d %H:%M} UTC.</p>")

    rows = "".join(
        f"<tr><td>{i.userid}</td><td>{i.display_name}</td><td>{i.action}</td>"
        f"<td>{i.risk.value}</td><td>{'; '.join(i.risk_reasons)}</td></tr>"
        for i in approval_request.items)
    return HTMLResponse(f"""
<h1>ALM access provisioning - {approval_request.environment}</h1>
<p>{approval_request.user_count} user(s), {approval_request.work_item_count} work item(s).
   Expires {approval_request.expires_at:%Y-%m-%d %H:%M} UTC.</p>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>User</th><th>Name</th><th>Action</th><th>Risk</th><th>Flags</th></tr>{rows}
</table>
<p>
  <a href="?decision=approve&token={token}">Approve all</a> |
  <a href="?decision=reject&token={token}">Reject</a>
</p>""")


@app.post("/approvals/{thread_id}")
async def submit_approval(thread_id: str, payload: ApprovalPayload,
                          request: Request) -> dict:
    """Record a decision and resume the parked run."""
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
        # Neither a signed token nor an IAP-authenticated identity: there would
        # be nobody to name in the audit row, so there is no approval to record.
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
    decision = ApprovalDecision(thread_id=thread_id, approved=approved,
                                approver=approver, plan_hash=plan_hash,
                                comment=comment,
                                approved_userids=approved_userids or [])
    await runtime.ctx.store.save_approval_decision(decision)
    log.info("approval_recorded", thread_id=thread_id, approved=approved,
             approver=approver)
    # Resuming is a background task: the graph may now run for half an hour.
    asyncio.create_task(_resume_in_background(thread_id, decision))
    return decision


# ---------------------------------------------------------------------- runs

@app.get("/runs")
async def list_runs(limit: int = 50) -> dict:
    return {"runs": await runtime.ctx.store.recent_runs(min(max(limit, 1), 200))}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    events = await runtime.ctx.store.run_events(run_id)
    if not events:
        raise HTTPException(status_code=404, detail="unknown run")
    return {"run_id": run_id, "events": events}


@app.post("/admin/reconcile", status_code=202)
async def force_reconcile(background: BackgroundTasks,
                          _identity: str = Depends(lambda: None)) -> dict:
    """Sweep the active queue now, without waiting for the timer."""
    thread_id = f"reconcile-{int(time.time())}"
    background.add_task(_run_in_background, thread_id=thread_id,
                        work_item_ids=None, trigger="reconcile")
    return {"accepted": True, "thread_id": thread_id}
