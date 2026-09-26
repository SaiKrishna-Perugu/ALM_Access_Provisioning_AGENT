"""The state the supervisor graph carries between nodes.

A plain TypedDict rather than a Pydantic model because LangGraph checkpoints it
to Postgres on every superstep and the reducers have to be cheap. The *values*
inside are Pydantic models, so nothing untyped survives a node boundary.

Reducers matter here. Nodes that fan out over users run concurrently, so
``results`` and ``events`` are appended to rather than replaced - a
last-write-wins reducer would silently drop one branch's audit rows, which is
precisely the class of bug this system cannot afford.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from alm_core.models import (
    ApprovalDecision,
    ApprovalRequest,
    ProvisionResult,
    RequestedUser,
    UserStatus,
    WorkItem,
)


def merge_status(left: dict[str, UserStatus],
                 right: dict[str, UserStatus]) -> dict[str, UserStatus]:
    """Merge per-user validation verdicts from concurrent branches."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def keep_last(left: Any, right: Any) -> Any:
    """Explicit last-write-wins, for scalars where that is actually correct."""
    return right if right is not None else left


class PipelineState(TypedDict, total=False):
    """Everything one provisioning run knows about itself."""

    # --- identity -----------------------------------------------------------
    run_id: str
    thread_id: str
    environment: str
    trigger: str                     # "webhook" | "reconcile" | "manual"
    # "dry-run" or "commit", fixed when the run starts. A resume must match it:
    # otherwise a dry run's preview approval could authorise real writes, or a
    # paused real run could silently become a dry run.
    run_mode: str

    # --- intake -------------------------------------------------------------
    work_item_ids: list[str]
    work_items: list[WorkItem]

    # --- extraction ---------------------------------------------------------
    users: list[RequestedUser]
    unparsed_rows: Annotated[list[str], operator.add]
    needs_human_extraction: bool

    # --- validation ---------------------------------------------------------
    statuses: Annotated[dict[str, UserStatus], merge_status]
    blocked_userids: list[str]

    # --- approval -----------------------------------------------------------
    approval_request: ApprovalRequest | None
    approval: ApprovalDecision | None
    plan_hash: str

    # --- execution ----------------------------------------------------------
    results: Annotated[list[ProvisionResult], operator.add]
    evidence_paths: dict[str, str]
    verified_userids: list[str]
    unverified_userids: list[str]

    # --- agentic orchestration ----------------------------------------------
    # Only used by the agentic graph; the deterministic graph ignores them.
    board: dict                      # Blackboard.to_state(), so a parked run survives
    agent_history: Annotated[list[dict], operator.add]
    next_agent: str
    next_task: str
    hops: int
    handoff_hint: str
    policy: dict

    # --- control ------------------------------------------------------------
    errors: Annotated[list[dict], operator.add]
    halted: bool
    halt_reason: str


def new_state(run_id: str, thread_id: str, environment: str,
              trigger: str = "manual", work_item_ids: list[str] | None = None,
              run_mode: str = "") -> PipelineState:
    return PipelineState(
        run_id=run_id,
        thread_id=thread_id,
        environment=environment,
        run_mode=run_mode,
        trigger=trigger,
        work_item_ids=work_item_ids or [],
        work_items=[],
        users=[],
        unparsed_rows=[],
        needs_human_extraction=False,
        statuses={},
        blocked_userids=[],
        approval_request=None,
        approval=None,
        plan_hash="",
        results=[],
        evidence_paths={},
        verified_userids=[],
        unverified_userids=[],
        board={},
        agent_history=[],
        next_agent="",
        next_task="",
        hops=0,
        handoff_hint="",
        policy={},
        errors=[],
        halted=False,
        halt_reason="",
    )


def halt(reason: str) -> PipelineState:
    """A partial state update that stops the run at the next conditional edge."""
    return PipelineState(halted=True, halt_reason=reason)
