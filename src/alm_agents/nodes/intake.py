"""Intake agent: turn a trigger into a normalised, deduplicated set of work items.

No LLM. A webhook names one work item; the reconciliation poll sweeps the whole
active queue. Both arrive here and leave as the same shape, so nothing
downstream needs to know which one fired.

Deduplication happens on the way in because EWM/RTC has no real webhook and the
reconciliation poll exists precisely to catch what the trigger missed - the two
overlap constantly, and the idempotency ledger should be the second line of
defence, not the first.
"""
from __future__ import annotations

from alm_core.errors import AlmError
from alm_core.logging import get_logger
from alm_core.tools import ewm
from alm_core.tools.base import ToolContext

from ..state import PipelineState, halt

log = get_logger("alm.agents.intake")


def make_intake_node(ctx: ToolContext):
    async def intake(state: PipelineState) -> PipelineState:
        requested = list(dict.fromkeys(state.get("work_item_ids") or []))
        try:
            if requested:
                items = []
                for work_item_id in requested:
                    item = await ewm.fetch_work_item(ctx, work_item_id)
                    if item is None:
                        log.warning("work_item_not_found", work_item=work_item_id)
                        continue
                    items.append(item)
            else:
                items = await ewm.fetch_open_requests(ctx)
        except AlmError as err:
            log.error("intake_failed", error=err.message, retryable=err.retryable)
            return {**halt(f"intake failed: {err.message}"),
                    "errors": [err.as_dict()]}

        # A work item with no parseable users still counts as seen; extraction
        # decides whether it needs the fallback or a human.
        seen: dict[str, object] = {}
        for item in items:
            seen.setdefault(item.work_item_id, item)
        work_items = [seen[k] for k in sorted(seen)]

        log.info("intake_complete", trigger=state.get("trigger", "manual"),
                 work_items=len(work_items),
                 users=sum(len(i.users) for i in work_items))
        return PipelineState(work_items=work_items,
                             work_item_ids=[i.work_item_id for i in work_items])

    return intake
