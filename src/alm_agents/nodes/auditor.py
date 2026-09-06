"""Auditor: close the run with a report that does not overstate itself.

The old summary counted "skipped" as success, so a dry run announced
SUCCESS (17) although nothing had happened. Here the five outcomes stay
separate, shadow-mode runs are labelled as plans, and users the run never
reached are reported as not attempted rather than as failures inherited from
somebody else's error.
"""
from __future__ import annotations

from alm_core.logging import get_logger
from alm_core.models import AuditEvent, Outcome

from ..state import PipelineState

log = get_logger("alm.agents.auditor")


def summarise(state: PipelineState) -> dict:
    """Per-user outcome roll-up for the run."""
    results = state.get("results") or []
    users = {u.userid for u in state.get("users") or []}
    worst: dict[str, Outcome] = {}
    severity = {Outcome.OK: 0, Outcome.SKIPPED: 1, Outcome.NOT_ATTEMPTED: 2,
                Outcome.TIMEOUT: 3, Outcome.FAILED: 4}

    for result in results:
        current = worst.get(result.userid, Outcome.OK)
        if severity[result.outcome] >= severity[current]:
            worst[result.userid] = result.outcome
    for userid in users:
        worst.setdefault(userid, Outcome.NOT_ATTEMPTED)

    buckets: dict[str, list[str]] = {o.value: [] for o in Outcome}
    for userid, outcome in sorted(worst.items()):
        buckets[outcome.value].append(userid)

    return {
        "run_id": state.get("run_id", ""),
        "thread_id": state.get("thread_id", ""),
        "environment": state.get("environment", ""),
        "trigger": state.get("trigger", ""),
        "shadow": any("shadow mode" in r.message for r in results),
        "halted": bool(state.get("halted")),
        "halt_reason": state.get("halt_reason", ""),
        "work_items": len(state.get("work_item_ids") or []),
        "users_total": len(users),
        "by_outcome": {k: v for k, v in buckets.items() if v},
        "verified": list(state.get("verified_userids") or []),
        "unverified": list(state.get("unverified_userids") or []),
        "blocked": list(state.get("blocked_userids") or []),
        "unparsed_rows": len(state.get("unparsed_rows") or []),
        "replayed": [r.userid for r in results if r.replayed],
        "errors": state.get("errors") or [],
    }


def make_auditor_node(ctx):
    async def auditor(state: PipelineState) -> PipelineState:
        report = summarise(state)
        await ctx.store.record(AuditEvent(
            run_id=state.get("run_id", ""), thread_id=state.get("thread_id", ""),
            environment=state.get("environment", ""), step="run_summary",
            outcome=Outcome.FAILED if report["by_outcome"].get("failed")
            else Outcome.OK,
            approver=(state.get("approval").approver if state.get("approval") else ""),
            message=("run halted: " + report["halt_reason"]) if report["halted"]
            else "run complete",
            detail=report))
        log.info("run_summary", **{k: v for k, v in report.items()
                                   if k not in ("errors", "by_outcome")})
        return PipelineState()

    return auditor
