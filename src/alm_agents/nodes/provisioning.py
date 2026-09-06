"""JTS provisioning and AD provisioning, plus the permission verification.

No LLM anywhere in this file - these are the writes.

The AD step does not perform the change: it enqueues a job for the domain-joined
Windows worker, because Kerberos SSO through a real browser cannot run in a
Linux container. Enqueuing is therefore *not* provisioning, and this module is
careful never to report it as such. The permission poll is the independent
confirmation that access actually landed.
"""
from __future__ import annotations

import asyncio

from alm_core.logging import get_logger
from alm_core.models import AuditEvent, Outcome, UserState
from alm_core.tools import gpt_queue, jts
from alm_core.tools.base import ToolContext, gather_per_user
from alm_core.tools.jts import check_role

from ..state import PipelineState

log = get_logger("alm.agents.provisioning")


def _approved_users(state: PipelineState):
    """Users the human actually approved, minus the ones that cannot be done."""
    decision = state.get("approval")
    statuses = state.get("statuses") or {}
    blocked = set(state.get("blocked_userids") or [])
    for user in state.get("users") or []:
        if user.userid in blocked:
            continue
        if decision is not None and not decision.covers(user.userid):
            continue
        if user.userid in statuses:
            yield user, statuses[user.userid]


def make_jts_node(ctx: ToolContext):
    async def jts_provision(state: PipelineState) -> PipelineState:
        pairs = list(_approved_users(state))
        if not pairs:
            return PipelineState(results=[])
        results = await gather_per_user(
            [jts.provision_user(ctx, user, status) for user, status in pairs])
        log.info("jts_provision_complete",
                 ok=sum(1 for r in results if r.succeeded),
                 failed=sum(1 for r in results if not r.succeeded))
        return PipelineState(results=results)

    return jts_provision


def make_ad_node(ctx: ToolContext, *, group: str, domain: str):
    async def ad_provision(state: PipelineState) -> PipelineState:
        # Only users whose JTS side is in order are worth an AD job.
        failed = {r.userid for r in (state.get("results") or []) if not r.succeeded}
        pairs = [(u, s) for u, s in _approved_users(state) if u.userid not in failed]
        if not pairs:
            return PipelineState(results=[])

        results = await gather_per_user([
            gpt_queue.request_group_membership(ctx, user, group=group, domain=domain)
            for user, _status in pairs])
        log.info("ad_jobs_enqueued", count=sum(1 for r in results if r.succeeded),
                 group=group)
        return PipelineState(results=results)

    return ad_provision


def make_verification_node(ctx: ToolContext):
    """Poll until every approved user holds the repository role, or time out.

    Runs inside the graph rather than blocking a terminal: the wait is cheap
    here because the run is a checkpointed coroutine, not an operator sitting in
    front of a console for half an hour.
    """

    async def verify(state: PipelineState) -> PipelineState:
        candidates = [u.userid for u, s in _approved_users(state)
                      if s.state != UserState.MISSING]
        failed = {r.userid for r in (state.get("results") or []) if not r.succeeded}
        candidates = [uid for uid in candidates if uid not in failed]
        if not candidates:
            return PipelineState(verified_userids=[], unverified_userids=[])

        wait_minutes = 0 if ctx.shadow else ctx.settings.permission_wait_minutes
        interval = max(1, ctx.settings.permission_interval_minutes)
        attempts_allowed = (wait_minutes // interval + 1) if wait_minutes else 1

        verified: set[str] = set()
        attempt = 0
        while attempt < attempts_allowed:
            attempt += 1
            pending = [uid for uid in candidates if uid not in verified]
            checks = await asyncio.gather(
                *(check_role(ctx, uid) for uid in pending), return_exceptions=True)
            for uid, ok in zip(pending, checks, strict=True):
                if ok is True:
                    verified.add(uid)
            remaining = [uid for uid in candidates if uid not in verified]
            log.info("permission_check", attempt=attempt, verified=len(verified),
                     pending=len(remaining))
            if not remaining or attempt >= attempts_allowed:
                break
            await asyncio.sleep(interval * 60)

        unverified = [uid for uid in candidates if uid not in verified]
        await ctx.store.record_many(
            [AuditEvent(run_id=ctx.run_id, thread_id=ctx.thread_id,
                        environment=ctx.environment, step="verification", userid=uid,
                        outcome=Outcome.OK,
                        message=f"{ctx.settings.jazz_role} confirmed after {attempt} check(s)")
             for uid in sorted(verified)]
            + [AuditEvent(run_id=ctx.run_id, thread_id=ctx.thread_id,
                          environment=ctx.environment, step="verification", userid=uid,
                          outcome=Outcome.TIMEOUT if wait_minutes else Outcome.SKIPPED,
                          message=(f"no {ctx.settings.jazz_role} after {attempt} check(s)"
                                   if wait_minutes else
                                   "single check - shadow mode does not poll"))
               for uid in unverified])

        log.info("verification_complete", verified=len(verified),
                 unverified=len(unverified), attempts=attempt)
        return PipelineState(verified_userids=sorted(verified),
                             unverified_userids=unverified)

    return verify
