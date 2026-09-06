"""Evidence agent: capture proof, validate it, attach it - in that order.

Only VERIFIED users get evidence. A screenshot of a profile whose permission has
not propagated proves nothing, and attaching it would assert something the run
has not established.

The batch invariant is a hard gate: if two users' artifacts are identical the
capture mechanism is broken, and nothing is uploaded at all. That is the check
that would have stopped seventeen copies of the JTS login page being attached to
production work items and reported as success.
"""
from __future__ import annotations

import os
import tempfile

from alm_core.errors import EvidenceInvalid
from alm_core.logging import get_logger
from alm_core.models import AuditEvent, Outcome
from alm_core.tools import evidence as evidence_tool
from alm_core.tools import ewm
from alm_core.tools.base import ToolContext, gather_per_user

from ..state import PipelineState

log = get_logger("alm.agents.evidence")


def make_evidence_node(ctx: ToolContext, shots_dir: str = ""):
    async def evidence(state: PipelineState) -> PipelineState:
        verified = list(state.get("verified_userids") or [])
        if not verified:
            log.info("evidence_skipped", reason="no verified user")
            return PipelineState(evidence_paths={})

        out_dir = shots_dir or os.path.join(
            tempfile.gettempdir(), "alm-evidence", state["run_id"])

        try:
            artifacts = await evidence_tool.capture_profiles(ctx, verified, out_dir)
        except EvidenceInvalid as err:
            # Deliberately fatal for the whole step: the remaining artifacts
            # cannot be trusted either, however plausible they look.
            log.error("evidence_gate_failed", problems=err.context.get("problems"))
            await ctx.store.record_many([
                AuditEvent(run_id=ctx.run_id, thread_id=ctx.thread_id,
                           environment=ctx.environment, step="evidence", userid=uid,
                           outcome=Outcome.FAILED, error_type="EvidenceInvalid",
                           message="; ".join(err.context.get("problems", []))[:500])
                for uid in verified])
            return PipelineState(evidence_paths={},
                                 errors=[err.as_dict()])
        except Exception as err:  # noqa: BLE001 - browser automation is brittle
            log.exception("evidence_capture_failed")
            await ctx.store.record_many([
                AuditEvent(run_id=ctx.run_id, thread_id=ctx.thread_id,
                           environment=ctx.environment, step="evidence", userid=uid,
                           outcome=Outcome.FAILED, error_type=type(err).__name__,
                           message=str(err)[:500])
                for uid in verified])
            return PipelineState(evidence_paths={},
                                 errors=[{"type": type(err).__name__, "message": str(err)}])

        # Users whose profile could not be confirmed simply get no evidence -
        # they are recorded as not attempted, never as failures caused by
        # somebody else's error.
        missing = [uid for uid in verified if uid not in artifacts]
        if missing:
            await ctx.store.record_many([
                AuditEvent(run_id=ctx.run_id, thread_id=ctx.thread_id,
                           environment=ctx.environment, step="evidence", userid=uid,
                           outcome=Outcome.NOT_ATTEMPTED,
                           message="profile could not be confirmed; nothing attached")
                for uid in missing])

        users_by_id = {u.userid: u for u in state.get("users") or []}
        tasks = []
        for userid, path in sorted(artifacts.items()):
            user = users_by_id.get(userid)
            if user is None:
                continue
            for work_item_id in user.work_item_ids:
                tasks.append(ewm.attach_evidence(
                    ctx, work_item_id=work_item_id, userid=userid,
                    path=path, filename=f"{userid}.png"))

        results = await gather_per_user(tasks)
        log.info("evidence_attached", artifacts=len(artifacts),
                 attachments=sum(1 for r in results if r.succeeded))
        return PipelineState(evidence_paths=artifacts, results=results)

    return evidence
