"""Shared plumbing for every tool: context, the write guard, shadow mode.

Every write in this system goes through :func:`guarded_write`. That is the
single place where four rules are enforced, so no tool can forget one:

1. **Shadow mode writes nothing.** The pilot runs read-and-plan first; the
   guard turns every write into a recorded no-op rather than relying on each
   tool to check a flag.
2. **An approval must cover this user.** A write with no matching
   ``ApprovalDecision`` raises rather than proceeding. The LLM cannot approve.
3. **The write is claimed before it is attempted** and completed afterwards, so
   a replayed webhook returns the original result instead of writing twice.
4. **Every attempt produces an audit row**, including the ones that fail and
   the ones that were replays.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..errors import AlmError, ApprovalRequired, IdempotencyViolation
from ..logging import get_logger
from ..models import (
    ApprovalDecision,
    AuditEvent,
    Operation,
    Outcome,
    ProvisionResult,
    idempotency_key,
)

log = get_logger("alm.tools")


@dataclass
class ToolContext:
    """Everything a tool needs, assembled once per run."""

    settings: object
    client: object            # alm_core.auth.JazzClient
    store: object             # alm_core.store.Store
    run_id: str
    thread_id: str = ""
    approval: ApprovalDecision | None = None
    # Bounds concurrent writes against the shared corporate systems.
    semaphore: asyncio.Semaphore | None = field(default=None, repr=False)

    def limiter(self) -> asyncio.Semaphore:
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(
                getattr(self.settings, "max_concurrent_writes", 4))
        return self.semaphore

    @property
    def environment(self) -> str:
        return getattr(self.settings, "environment", "")

    @property
    def shadow(self) -> bool:
        return bool(getattr(self.settings, "shadow_mode", True))

    @property
    def approver(self) -> str:
        return self.approval.approver if self.approval else ""


async def record(ctx: ToolContext, result: ProvisionResult, step: str = "") -> None:
    await ctx.store.record(AuditEvent.from_result(
        result, run_id=ctx.run_id, thread_id=ctx.thread_id,
        environment=ctx.environment, step=step, approver=ctx.approver))


async def guarded_write(
    ctx: ToolContext,
    *,
    userid: str,
    work_item_id: str,
    operation: Operation,
    action: Callable[[], Awaitable[tuple[bool, str, dict]]],
    step: str = "",
) -> ProvisionResult:
    """Run one write under shadow mode, approval, idempotency and audit.

    ``action`` returns ``(ok, message, detail)`` and is only awaited when all
    four guards pass. It must perform exactly one logical write.
    """
    key = idempotency_key(work_item_id, userid, operation)
    step = step or operation.value
    base = {"userid": userid, "operation": operation, "work_item_id": work_item_id,
            "idempotency_key": key}

    # 1. Shadow mode: plan it, never do it.
    if ctx.shadow:
        result = ProvisionResult(**base, outcome=Outcome.SKIPPED,
                                 message="shadow mode - not written")
        await record(ctx, result, step)
        return result

    # 2. Approval. An unapproved write is a bug, not a decision to make here.
    if ctx.approval is None or not ctx.approval.covers(userid):
        result = ProvisionResult(**base, outcome=Outcome.NOT_ATTEMPTED,
                                 message="no human approval covers this user")
        await record(ctx, result, step)
        raise ApprovalRequired(
            f"{operation.value} for {userid} is not covered by an approval",
            context={"work_item_id": work_item_id, "thread_id": ctx.thread_id})

    # 3. Idempotency claim.
    try:
        proceed, previous = await ctx.store.claim(
            key, run_id=ctx.run_id, work_item_id=work_item_id, userid=userid,
            operation=operation)
    except IdempotencyViolation as err:
        result = ProvisionResult(**base, outcome=Outcome.NOT_ATTEMPTED,
                                 message=str(err), detail=err.context)
        await record(ctx, result, step)
        return result

    if not proceed:
        replay = (previous.model_copy(update={"replayed": True}) if previous else
                  ProvisionResult(**base, outcome=Outcome.SKIPPED, replayed=True,
                                  message="already completed by an earlier run"))
        await record(ctx, replay, step)
        return replay

    # 4. Perform it, bounded, and always complete the claim.
    try:
        async with ctx.limiter():
            ok, message, detail = await action()
        result = ProvisionResult(
            **base, outcome=Outcome.OK if ok else Outcome.FAILED,
            message=message, detail=detail or {})
    except AlmError as err:
        result = ProvisionResult(**base, outcome=Outcome.FAILED, message=err.message,
                                 detail={"error": err.as_dict()})
    except Exception as err:  # noqa: BLE001 - one user must not kill the batch
        log.exception("tool_write_crashed", userid=userid, operation=operation.value)
        result = ProvisionResult(**base, outcome=Outcome.FAILED,
                                 message=f"{type(err).__name__}: {err}")

    await ctx.store.complete(key, result)
    await record(ctx, result, step)
    return result


async def to_thread(func, /, *args, **kwargs):
    """Run a blocking requests call without stalling the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


async def gather_per_user(coros: list[Awaitable[ProvisionResult]]) -> list[ProvisionResult]:
    """Await every write, letting one user's failure not cancel the others."""
    settled = await asyncio.gather(*coros, return_exceptions=True)
    results: list[ProvisionResult] = []
    for item in settled:
        if isinstance(item, BaseException):
            log.error("write_task_failed", error=str(item))
            continue
        results.append(item)
    return results
