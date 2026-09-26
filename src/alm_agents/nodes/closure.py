"""Closure agent: tell each work item what actually happened.

The comment is a permanent record on someone else's work item, so every line is
derived from what this run *recorded*, never from what it intended. The system
previously asserted "User added to JTS" for every user on the card, including
ten it had found already present and never touched - a factual misstatement
written into a production audit trail.

The LLM may rephrase a line. It cannot introduce one: the template is generated
first, the model's draft is checked against the recorded action, and any draft
claiming something the outcome does not support is discarded in favour of the
template.

The comment carries no signature or marker. A redelivered webhook or a retried
node cannot double it: the idempotency ledger records the post, and the post
itself is skipped when an identical comment is already on the work item.
"""
from __future__ import annotations

import os

from alm_core.logging import get_logger
from alm_core.models import Operation, Outcome, UserState
from alm_core.tools import ewm
from alm_core.tools.base import ToolContext, gather_per_user

from .. import llm
from ..state import PipelineState

log = get_logger("alm.agents.closure")

# The CLI's header and its override, so both tools post the same thing.
COMMENT_HEADER = os.getenv("ALM_COMMENT_HEADER", "ALM access provisioning result :")

# recorded action -> the sentence that is true about that user
ACTION_TEXT = {
    "created": "User added to JTS",
    "unarchived": "User reactivated in JTS (account was archived)",
    "already_active": "User already present in JTS - no change needed",
    "shadow": "User would be provisioned (shadow mode - nothing was written)",
    "unknown": "User access confirmed in JTS",
}


def _action_for(userid: str, state: PipelineState) -> str:
    """What this run recorded for a user, in ACTION_TEXT terms."""
    return action_from_records(userid, state.get("results") or [],
                               state.get("statuses") or {})


def action_from_records(userid: str, results, statuses) -> str:
    """The ACTION_TEXT key the records support for a user - never more."""
    for result in results:
        if result.userid != userid:
            continue
        if result.operation == Operation.JTS_CREATE and result.outcome == Outcome.OK:
            return "created"
        if result.operation == Operation.JTS_UNARCHIVE and result.outcome == Outcome.OK:
            return "unarchived"
        if result.operation == Operation.JTS_CREATE and result.outcome == Outcome.SKIPPED:
            return "shadow" if "shadow" in result.message else "already_active"
    status = statuses.get(userid)
    if status is not None and status.state == UserState.EXISTS:
        return "already_active"
    return "unknown"


def render_comment(entries: list[tuple[str, str, str]]) -> str:
    """The comment from records alone, in the CLI's exact line format.

    ``entries`` is [(userid, display_name, action)]. No model is involved, so
    the same outcome always renders the same text: the duplicate check can
    match it, and nothing in it can claim more than was recorded.
    """
    lines = [COMMENT_HEADER]
    for userid, display_name, action in entries:
        text = ACTION_TEXT.get(action, ACTION_TEXT["unknown"])
        lines.append(f"{userid}: {display_name}: {text} - (active)")
    return "\n".join(lines)


def build_comment(ctx: ToolContext, work_item_id: str,
                  entries: list[tuple[str, str, str]]) -> tuple[str, str]:
    """Render the comment. ``entries`` is [(userid, display_name, action)]."""
    lines = [COMMENT_HEADER]
    for userid, display_name, action in entries:
        template = f"{userid}: {display_name}: {ACTION_TEXT.get(action, ACTION_TEXT['unknown'])}"
        draft = llm.draft_comment_line(ctx.settings, userid=userid,
                                       display_name=display_name, action=action)
        # The template is always correct; a draft only replaces it if it passes
        # the claim check inside draft_comment_line.
        lines.append(f"{userid}: {draft}" if draft else template)
    return "\n".join(lines), ""


def make_closure_node(ctx: ToolContext):
    async def closure(state: PipelineState) -> PipelineState:
        verified = set(state.get("verified_userids") or [])
        if not verified:
            log.info("closure_skipped", reason="no verified user to report")
            return PipelineState(results=[])

        users_by_id = {u.userid: u for u in state.get("users") or []}
        # One comment per work item, listing every verified user on it.
        by_work_item: dict[str, list[tuple[str, str, str]]] = {}
        for userid in sorted(verified):
            user = users_by_id.get(userid)
            if user is None:
                continue
            action = _action_for(userid, state)
            for work_item_id in user.work_item_ids:
                by_work_item.setdefault(work_item_id, []).append(
                    (userid, user.display_name, action))

        tasks = []
        for work_item_id, entries in sorted(by_work_item.items()):
            text, marker = build_comment(ctx, work_item_id, entries)
            # The comment is attributed to the first user on it for audit
            # purposes; every user on the item still gets their own row from
            # the results list.
            tasks.append(ewm.post_comment(ctx, work_item_id=work_item_id,
                                          userid=entries[0][0], text=text,
                                          marker=marker))

        results = await gather_per_user(tasks)
        log.info("closure_complete", work_items=len(by_work_item),
                 posted=sum(1 for r in results if r.succeeded))
        return PipelineState(results=results)

    return closure
