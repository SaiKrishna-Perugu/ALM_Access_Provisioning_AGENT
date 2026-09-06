"""Extraction agent: regex first, LLM only for what the regex could not read.

The deterministic parser handles the well-formed field and is the only path that
runs for the overwhelming majority of work items. The model is reached only for
rows that failed to parse, sees redacted text, and produces *proposals* that are
validated against the contracts before they count.

Anything the model produced is flagged. That flag survives all the way to the
approval card, so a human always knows which user IDs a machine guessed.
"""
from __future__ import annotations

from alm_core.logging import get_logger
from alm_core.oslc import merge_users, parse_new_users
from alm_core.tools.base import ToolContext

from .. import llm
from ..state import PipelineState

log = get_logger("alm.agents.extraction")


def make_extraction_node(ctx: ToolContext):
    async def extraction(state: PipelineState) -> PipelineState:
        work_items = state.get("work_items") or []
        batches = [item.users for item in work_items]
        unparsed: list[str] = []
        llm_batches = []

        for item in work_items:
            if not item.new_users_raw:
                continue
            _records, rejected = parse_new_users(item.new_users_raw)
            if not rejected:
                continue
            unparsed.extend(f"{item.work_item_id}: {row}" for row in rejected)
            log.info("attempting_llm_extraction", work_item=item.work_item_id,
                     rejected_rows=len(rejected))
            proposed = llm.extract_users(
                ctx.settings, "; ".join(rejected), item.work_item_id, item.summary)
            # Never let the fallback re-propose a user the parser already read
            # correctly - that would downgrade a clean record to "LLM guessed".
            known = {u.userid for u in item.users}
            llm_batches.append([u for u in proposed if u.userid not in known])

        users = merge_users(batches + llm_batches)
        # A row nobody could read is not a user who quietly disappears: the run
        # carries it to the approval card as an explicit "needs a human".
        needs_human = bool(unparsed) and not any(u.extracted_by_llm for u in users)

        log.info("extraction_complete", users=len(users),
                 llm_proposed=sum(1 for u in users if u.extracted_by_llm),
                 unparsed_rows=len(unparsed))
        return PipelineState(users=users, unparsed_rows=unparsed,
                             needs_human_extraction=needs_human)

    return extraction
