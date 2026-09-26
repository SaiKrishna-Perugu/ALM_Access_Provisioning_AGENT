"""Approval gate: the run stops here until a human decides.

This is a LangGraph ``interrupt()``. The state is checkpointed to Postgres, the
container may be restarted or scaled to zero, and the run resumes exactly here
when the decision arrives through the API - which is what makes a 4-hour
approval latency cost nothing.

Three rules the gate enforces:

* **The plan is fingerprinted.** A decision only unblocks the batch it was shown.
  If the queue moved while the approver was thinking, the hash no longer matches
  and the run goes back for re-approval rather than executing work nobody saw.
  This is the cloud form of the defect that swept two unreviewed work items into
  a production commit.
* **Approvals expire.** An approval from yesterday does not authorise today's
  writes.
* **Auto-approval can never provision.** Even with ``auto_approve_low_risk``,
  a batch that would create or reactivate an account still goes to a person.
  Only comment-and-evidence runs can skip the wait.
"""
from __future__ import annotations

from datetime import timedelta

from alm_core.logging import get_logger
from alm_core.models import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalRequest,
    AuditEvent,
    Outcome,
    RiskLevel,
    UserState,
    plan_hash,
    utcnow,
)
from alm_core.tools.base import ToolContext

from ..state import PipelineState, halt

log = get_logger("alm.agents.approval")

_ACTION_FOR_STATE = {
    UserState.READY: "import into JTS",
    UserState.ARCHIVED: "reactivate archived account",
    UserState.EXISTS: "no change (already active)",
    UserState.MISSING: "cannot provision - not in LDAP",
    UserState.INVALID: "cannot provision - LDAP entry invalid",
    UserState.UNKNOWN: "unknown - validation did not complete",
}


def build_request(state: PipelineState, ctx: ToolContext) -> ApprovalRequest:
    """The card an approver sees: one line per user, with the risk flags."""
    statuses = state.get("statuses") or {}
    items: list[ApprovalItem] = []
    for user in state.get("users") or []:
        status = statuses.get(user.userid)
        if status is None:
            continue
        items.append(ApprovalItem(
            userid=user.userid,
            display_name=user.display_name,
            work_item_ids=user.work_item_ids,
            action=_ACTION_FOR_STATE.get(status.state, status.state.value),
            state=status.state,
            risk=status.risk,
            risk_reasons=status.risk_reasons,
        ))
    items.sort(key=lambda i: (i.risk != RiskLevel.HIGH, i.userid))

    ttl = timedelta(minutes=ctx.settings.approval_ttl_minutes)
    return ApprovalRequest(
        thread_id=state["thread_id"],
        run_id=state["run_id"],
        environment=ctx.environment,
        expires_at=utcnow() + ttl,
        plan_hash=plan_hash(items),
        items=items,
    )


def _auto_approvable(request: ApprovalRequest, ctx: ToolContext) -> bool:
    """True only for a batch that changes no account and carries no risk flag."""
    if not ctx.settings.auto_approve_low_risk:
        return False
    if ctx.settings.is_prod and not ctx.settings.shadow_mode:
        # Phase 9 enables this on TEST first. Production auto-approval is a
        # separate decision, made deliberately, not inherited from a flag.
        return False
    return all(item.state == UserState.EXISTS and item.risk == RiskLevel.LOW
               and not item.risk_reasons for item in request.items)


async def open_request(store, request: ApprovalRequest) -> tuple[ApprovalRequest, bool]:
    """Save the request, or return the one already pending for this exact plan.

    LangGraph re-executes an interrupted node from its first line when the run
    resumes. Without this, every resume would write a fresh request (moving the
    expiry forward, so an approval could never expire) and send the approver a
    second card for a decision they had already made.

    Returns ``(request, is_new)``; only a new request should be announced.
    """
    existing, decision = await store.get_approval(request.thread_id)
    if (existing is not None
            and existing.plan_hash == request.plan_hash
            and utcnow() <= existing.expires_at
            and (decision is None or decision.plan_hash != existing.plan_hash)):
        return existing, False
    await store.save_approval_request(request)
    return request, True


def make_approval_node(ctx: ToolContext, notifier=None):
    """``notifier(request)`` delivers the card; None means "record only"."""

    async def approval(state: PipelineState) -> PipelineState:
        request = build_request(state, ctx)
        if not request.items:
            return halt("nothing to approve - no user survived validation")

        request, is_new = await open_request(ctx.store, request)

        if _auto_approvable(request, ctx):
            decision = ApprovalDecision(
                thread_id=request.thread_id, approved=True,
                approver="auto:low-risk-policy", plan_hash=request.plan_hash,
                comment="auto-approved: no account is created or reactivated")
            await ctx.store.save_approval_decision(decision)
            await _record(ctx, request, decision, "auto_approved")
            ctx.approval = decision
            return PipelineState(approval_request=request, approval=decision,
                                 plan_hash=request.plan_hash)

        if notifier is not None and is_new:
            try:
                await notifier(request)
            except Exception as err:  # noqa: BLE001 - a card that fails to send
                # must not lose the run; the approval is still reachable by URL.
                log.warning("approval_notification_failed", error=str(err),
                            thread_id=request.thread_id)

        if is_new:
            log.info("awaiting_approval", thread_id=request.thread_id,
                     users=request.user_count, work_items=request.work_item_count,
                     plan_hash=request.plan_hash[:12])

        # The graph suspends here. On resume this node runs again from the top;
        # open_request() is what makes that second pass side-effect free.
        from langgraph.types import interrupt

        payload = interrupt({
            "type": "approval_required",
            "thread_id": request.thread_id,
            "plan_hash": request.plan_hash,
            "expires_at": request.expires_at.isoformat(),
            "items": [i.model_dump(mode="json") for i in request.items],
        })

        decision = _coerce(payload, request)
        if decision is None:
            return halt("approval payload was not a valid decision")

        if decision.plan_hash and decision.plan_hash != request.plan_hash:
            await _record(ctx, request, decision, "plan_changed")
            return halt(
                "the plan changed after it was approved - refusing to execute work "
                f"that was never reviewed (approved {decision.plan_hash[:12]}, "
                f"current {request.plan_hash[:12]})")

        if utcnow() > request.expires_at:
            await _record(ctx, request, decision, "expired")
            return halt(f"approval expired at {request.expires_at.isoformat()}")

        await ctx.store.save_approval_decision(decision)
        await _record(ctx, request, decision,
                      "approved" if decision.approved else "rejected")

        if not decision.approved:
            return {**halt(f"rejected by {decision.approver}"),
                    "approval_request": request, "approval": decision}

        ctx.approval = decision
        log.info("approval_granted", thread_id=request.thread_id,
                 approver=decision.approver,
                 users=len(decision.approved_userids) or request.user_count)
        return PipelineState(approval_request=request, approval=decision,
                             plan_hash=request.plan_hash)

    return approval


def _coerce(payload, request: ApprovalRequest) -> ApprovalDecision | None:
    """Accept a model, a dict, or a bare boolean from the resume value."""
    if isinstance(payload, ApprovalDecision):
        return payload
    if isinstance(payload, bool):
        return ApprovalDecision(thread_id=request.thread_id, approved=payload,
                                plan_hash=request.plan_hash, approver="unknown")
    if isinstance(payload, dict):
        payload.setdefault("thread_id", request.thread_id)
        try:
            return ApprovalDecision.model_validate(payload)
        except Exception as err:  # pydantic ValidationError
            log.error("invalid_approval_payload", error=str(err))
            return None
    return None


async def _record(ctx: ToolContext, request: ApprovalRequest,
                  decision: ApprovalDecision, outcome: str) -> None:
    await ctx.store.record(AuditEvent(
        run_id=request.run_id, thread_id=request.thread_id,
        environment=request.environment, step="approval",
        outcome=Outcome.OK if decision.approved else Outcome.SKIPPED,
        approver=decision.approver,
        message=outcome,
        detail={"plan_hash": request.plan_hash,
                "users": [i.userid for i in request.items],
                "approved_userids": decision.approved_userids,
                "comment": decision.comment}))
