"""FastAPI surface: triggers in, decisions in, runs out.

Endpoints:

    POST /webhooks/ewm              HMAC-authenticated trigger for one work item
    GET  /approvals/{thread}        the card, the votes, how many approvers it needs
    POST /approvals/{thread}        an approver's decision; the run resumes when
                                    the two-person rule is satisfied
    GET  /runs                      the run registry, newest first
    GET  /runs/{thread}             one run: registry row, approval, audit trail
    POST /runs/{thread}/stop        stop a run after its current step
    GET  /runs/{thread}/trace       the run's trace records (cursor-paged)
    POST /runs                      start a run (operator)
    GET  /queue                     job counts by status, and dead jobs
    POST /admin/reconcile           queue a reconciliation sweep now (admin)
    GET  /auth/login /auth/callback /auth/logout   OIDC sign-in (ALM_AUTH_MODE=oidc)
    GET  /me                        who you are and what you may do
    GET  /status                    database, EWM, JTS and model health (viewer)
    GET  /  and /api/*              the web console (``alm_api.console``)
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
from urllib.parse import urlsplit

from alm_core.credentials import build_resolver
from alm_core.logging import configure, get_logger
from alm_core.models import AuditEvent, Outcome

from . import auth
from .security import caller_identity, verify_webhook

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse, RedirectResponse
    from pydantic import BaseModel, Field
except ImportError as err:  # pragma: no cover
    raise ImportError("alm_api needs the cloud extras: "
                      "pip install -r requirements-cloud.txt") from err

log = get_logger("alm.api")


class ApprovalPayload(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=500)
    approved_userids: list[str] = Field(default_factory=list, max_length=200)


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
        origin = request.headers.get("origin", "")
        if (request.method not in ("GET", "HEAD") and origin and
                urlsplit(origin).netloc != request.headers.get("host", "")):
            # A browser on another site riding the proxy's or our session cookie.
            raise HTTPException(status_code=403, detail="request from another site")
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
    runtime.resolver = build_resolver(settings, interactive=False)

    stack = contextlib.AsyncExitStack()
    runtime.services = await stack.enter_async_context(
        build_services(settings, notifier=make_notifier(settings, runtime.resolver),
                       resolver=runtime.resolver))
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
        raise HTTPException(status_code=404, detail="no approval for that run")
    return request, decision


async def _approval_view(thread_id: str) -> dict:
    from alm_agents import approval_policy

    request, decision = await _load_approval(thread_id)
    needed = approval_policy.approvers_needed(request, runtime.settings)
    votes = await runtime.store.votes(thread_id, request.plan_hash)
    return {"card": request.model_dump(mode="json"), "needed": needed, "votes": votes,
            "tally": approval_policy.tally(votes, needed).public(),
            "decision": decision.model_dump(mode="json") if decision else None}


@app.get("/approvals/{thread_id}")
async def approval(thread_id: str, _user=Depends(require("viewer"))) -> dict:
    """The card, the votes so far, and how many approvers it needs."""
    return await _approval_view(thread_id)


@app.post("/approvals/{thread_id}")
async def vote(thread_id: str, payload: ApprovalPayload,
               user=Depends(require("approver"))) -> dict:
    """One approver's decision. The run resumes once the policy is satisfied:
    enough distinct approvers, none of them the person who started a run that
    needs two, or one rejection."""
    from alm_agents import approval_policy
    from alm_agents.worker import submit_decision

    request, decided = await _load_approval(thread_id)
    if decided is not None:
        raise HTTPException(status_code=409, detail=f"already decided by {decided.approver}")
    run = await runtime.store.get_run(thread_id) or {}
    if run and run.get("status") != "awaiting_approval":
        raise HTTPException(status_code=409, detail="this run is not waiting for a decision")
    needed = approval_policy.approvers_needed(request, runtime.settings)
    votes = await runtime.store.votes(thread_id, request.plan_hash)
    try:
        approval_policy.check_vote(approver=user.identity,
                                   requested_by=run.get("requested_by", ""),
                                   needed=needed, votes=votes)
    except approval_policy.VoteRefused as err:
        raise HTTPException(status_code=403, detail=str(err)) from None
    shown = [i.userid for i in request.items]
    chosen = [u for u in shown if u.upper() in {x.upper() for x in payload.approved_userids}]
    if payload.approved and not chosen:
        raise HTTPException(status_code=422, detail="tick at least one user, or reject")
    if not await runtime.store.add_vote(thread_id, request.plan_hash, user.identity,
                                        approved=payload.approved, userids=chosen,
                                        comment=payload.comment):
        raise HTTPException(status_code=409, detail="you have already decided this card")
    await runtime.store.record(AuditEvent(
        run_id=request.run_id, thread_id=thread_id, environment=request.environment,
        step="approval_vote", outcome=Outcome.OK if payload.approved else Outcome.SKIPPED,
        approver=user.identity, message="approved" if payload.approved else "rejected",
        detail={"userids": chosen, "plan_hash": request.plan_hash,
                "comment": payload.comment[:200]}))
    log.info("approval_vote", thread_id=thread_id, approver=user.identity,
             approved=payload.approved)

    tally = approval_policy.tally(await runtime.store.votes(thread_id, request.plan_hash),
                                  needed)
    if tally.complete:
        decision = approval_policy.decision(tally, thread_id=thread_id,
                                            plan_hash=request.plan_hash, shown=shown)
        await runtime.store.save_approval_decision(decision)
        # The resume is a job: whichever worker claims it continues the run.
        await submit_decision(runtime.store, thread_id, decision)
        log.info("approval_decided", thread_id=thread_id, approved=decision.approved,
                 approvers=decision.approver)
    return await _approval_view(thread_id)


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


# ------------------------------------------------------------------- status

STATUS_OK_SECONDS = 60
# A failed sign-in is not retried for ten minutes: probing every minute with a
# wrong password would lock the service account.
STATUS_FAILED_SECONDS = 600
_status: dict = {}


async def _check(name: str, probe) -> dict:
    cached = _status.get(name)
    if cached and time.time() < cached["until"]:
        return cached["result"]
    started = time.perf_counter()
    try:
        detail = await probe()
        result = {"ok": True, "detail": detail or "ok"}
    except Exception as err:  # noqa: BLE001 - reported, never raised
        from alm_core.logging import scrub_secrets

        result = {"ok": False, "detail": scrub_secrets(f"{type(err).__name__}: {err}")[:300]}
    result["ms"] = int((time.perf_counter() - started) * 1000)
    _status[name] = {"result": result, "until": time.time() + (
        STATUS_OK_SECONDS if result["ok"] else STATUS_FAILED_SECONDS)}
    return result


@app.get("/status")
async def status(_user=Depends(require("viewer"))) -> dict:
    """What this deployment can reach: the database, EWM, JTS, the model.
    Unlike /readyz, a dependency being down does not take the API out of
    rotation - people still need to see their runs."""
    services, settings = runtime.services, runtime.settings

    async def database():
        depth = await runtime.store.queue_depth()
        return f"queue {depth}"

    def jazz(server: str, kind: str):
        async def probe():
            if not server:
                raise RuntimeError(f"{kind.upper()} server is not configured")
            # Reuses the signed-in session; signs in only when there is none.
            await asyncio.to_thread(services.client.session, server, kind=kind)
            return urlsplit(server).hostname
        return probe

    async def model():
        if getattr(services, "agent_llm", None) is None:
            if getattr(settings, "orchestration", "") == "deterministic":
                return "not used (deterministic orchestration)"
            raise RuntimeError("no model client could be built; check the provider settings")
        return f"{settings.llm_provider}:{settings.agent_model}"

    checks = {"database": await _check("database", database),
              "ewm": await _check("ewm", jazz(settings.ewm_server, "ewm")),
              "jts": await _check("jts", jazz(settings.jts_server, "jts")),
              "model": await _check("model", model)}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks,
            "environment": settings.environment, "writes": not settings.shadow_mode}


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


# ------------------------------------------------------------------ console

from .console import router as console_router  # noqa: E402 - routes use this module

app.include_router(console_router)
