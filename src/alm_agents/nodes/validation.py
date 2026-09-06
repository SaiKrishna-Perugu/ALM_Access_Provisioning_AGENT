"""Validation agent: what is actually true about each user, and how risky.

No LLM. Every verdict comes from LDAP and the JTS registry. This is also where
the run learns what it will *not* be able to do - a user with no LDAP entry
cannot be provisioned however many times it is retried, so they are separated
out here rather than failing repeatedly later.

Users are checked concurrently but bounded, because EWM and JTS are shared
corporate systems and an autonomous agent's retry storm is somebody else's
outage.
"""
from __future__ import annotations

import asyncio

from alm_core.errors import AlmError
from alm_core.logging import get_logger
from alm_core.models import AuditEvent, Outcome, UserState, UserStatus
from alm_core.tools import jts
from alm_core.tools.base import ToolContext

from ..state import PipelineState

log = get_logger("alm.agents.validation")


def make_validation_node(ctx: ToolContext):
    async def validation(state: PipelineState) -> PipelineState:
        users = state.get("users") or []
        if not users:
            return PipelineState(statuses={}, blocked_userids=[])

        limiter = asyncio.Semaphore(ctx.settings.max_concurrent_writes)

        async def classify(user):
            async with limiter:
                try:
                    return user.userid, await jts.classify_user(ctx, user)
                except AlmError as err:
                    log.warning("validation_failed", userid=user.userid,
                                error=err.message)
                    return user.userid, UserStatus(
                        userid=user.userid, state=UserState.UNKNOWN,
                        risk_reasons=[f"validation failed: {err.message}"])

        settled = await asyncio.gather(*(classify(u) for u in users),
                                       return_exceptions=True)
        statuses: dict[str, UserStatus] = {}
        errors: list[dict] = []
        for item in settled:
            if isinstance(item, BaseException):
                errors.append({"type": type(item).__name__, "message": str(item)})
                continue
            userid, status = item
            statuses[userid] = status

        blocked = sorted(uid for uid, s in statuses.items() if s.blocked)
        already = sorted(uid for uid, s in statuses.items()
                         if s.state == UserState.EXISTS and s.has_role)

        # Record the verdicts now: if the run is later abandoned at the approval
        # gate, the audit still shows what was known and when.
        await ctx.store.record_many([
            AuditEvent(run_id=ctx.run_id, thread_id=ctx.thread_id,
                       environment=ctx.environment, step="validation",
                       userid=uid, outcome=Outcome.OK,
                       message=f"state={status.state.value} risk={status.risk.value}",
                       detail={"risk_reasons": status.risk_reasons,
                               "has_role": status.has_role})
            for uid, status in sorted(statuses.items())])

        log.info("validation_complete", users=len(statuses), blocked=len(blocked),
                 already_provisioned=len(already))
        return PipelineState(statuses=statuses, blocked_userids=blocked, errors=errors)

    return validation
