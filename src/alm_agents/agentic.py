"""The agentic graph: a supervisor loop over autonomous, tool-calling agents.

Structurally this is three nodes and a cycle, not a pipeline:

    START -> supervisor -> agent -> supervisor -> agent -> ... -> auditor
                        \\-> approval (interrupt) -/

The supervisor picks who acts. The agent acts - deciding its own tool calls -
and reports back. The supervisor picks again. The order is not known in advance
and differs between runs, which is the point.

The approval gate sits *inside* the loop rather than at a fixed position: an
agent decides when the batch is ready for a human, the graph interrupts, and the
run resumes into the supervisor with an approval in hand. That means a batch
that turns out to need nothing (every user already active) never bothers anybody,
and a batch that grows mid-run can be sent back for a second approval.

Durability is what makes this affordable. Every hop is checkpointed, including
the blackboard, so a run can pause for hours at the gate and resume in a
different container with everything the planning agents established intact.
"""
from __future__ import annotations

from datetime import timedelta

from alm_core.logging import get_logger
from alm_core.models import (
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    Outcome,
    plan_hash,
    utcnow,
)
from alm_core.tools.base import ToolContext

from .agent import AgentRunner, summarise_calls
from .memory import MemoryStore
from .nodes.approval import open_request
from .nodes.auditor import summarise
from .policy import PolicyEngine
from .roster import ROSTER
from .state import PipelineState, halt
from .supervisor import MAX_HOPS, decide, honour_handoff
from .toolkit import Backend, Blackboard, build_registry, context_for

log = get_logger("alm.agentic")


class AgenticRuntime:
    """Holds the per-process objects the graph nodes close over."""

    def __init__(self, ctx: ToolContext, *, llm, supervisor_llm=None,
                 memory: MemoryStore | None = None, max_hops: int = MAX_HOPS,
                 notifier=None, shots_dir: str = "", backend: Backend | None = None,
                 on_event=None):
        self.ctx = ctx
        self.llm = llm
        self.supervisor_llm = supervisor_llm or llm
        self.memory = memory or MemoryStore(getattr(ctx, "store", None))
        self.max_hops = max_hops
        self.notifier = notifier
        # on_event(kind, data): live progress for a terminal or a UI. Optional.
        self.on_event = on_event
        self.board = Blackboard()
        self.registry = build_registry(ctx, self.board, self.memory,
                                       shots_dir=shots_dir, backend=backend)
        self.policy = PolicyEngine(
            shadow=ctx.shadow,
            environment=ctx.environment,
            max_writes=getattr(ctx.settings, "max_writes_per_run", 50),
            max_tool_calls=getattr(ctx.settings, "max_tool_calls_per_run", 400),
        )

    def sync_from(self, state: PipelineState) -> None:
        """Rehydrate after a resume in a fresh process."""
        self.board.load_state(state.get("board"))
        # Scope comes from the run's own state, which only the caller sets.
        self.board.scope = set(state.get("work_item_ids") or [])
        approval = state.get("approval")
        if approval is not None:
            self.policy.approval = approval
            self.ctx.approval = approval
        # Policy state that must survive a resume in a new process: the PROD
        # confirmation and the run's budgets. Counters only ever move forward.
        saved = state.get("policy") or {}
        self.policy.prod_confirmed = self.policy.prod_confirmed or bool(
            saved.get("prod_confirmed"))
        self.policy.tool_calls = max(self.policy.tool_calls,
                                     int(saved.get("tool_calls") or 0))
        self.policy.writes_performed = max(self.policy.writes_performed,
                                           int(saved.get("writes") or 0))
        self.ctx.run_id = state.get("run_id", "") or self.ctx.run_id
        self.ctx.thread_id = state.get("thread_id", "") or self.ctx.thread_id

    def emit(self, kind: str, **data) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:  # noqa: BLE001 - a display hook must not stop a run
            log.exception("runtime_event_hook_failed", kind=kind)

    def snapshot(self, state: PipelineState) -> dict:
        """What the supervisor sees when it chooses."""
        return {
            "work_items": sorted(self.board.work_items),
            "users": {u: (self.board.statuses[u].state.value
                          if u in self.board.statuses else "not validated")
                      for u in sorted(self.board.users)},
            "verified": sorted(self.board.verified),
            "writes_recorded": len(self.board.results),
            "evidence_captured": sorted(self.board.evidence),
            "approval_requested": self.board.approval_requested,
            "approved": state.get("approval") is not None
            and getattr(state.get("approval"), "approved", False),
            "scope": sorted(self.board.scope) or "the whole active queue",
            "hops_used": state.get("hops", 0),
            "hops_remaining": self.max_hops - state.get("hops", 0),
        }


# ------------------------------------------------------------------ the nodes

def make_supervisor_node(runtime: AgenticRuntime):
    async def supervisor(state: PipelineState) -> PipelineState:
        runtime.sync_from(state)
        hops = state.get("hops", 0)

        if hops >= runtime.max_hops:
            log.warning("hop_budget_exhausted", hops=hops)
            return {**halt(f"the run used its {runtime.max_hops} routing hops"),
                    "next_agent": "DONE"}

        snapshot = runtime.snapshot(state)
        decision = await decide(
            runtime.supervisor_llm,
            history=state.get("agent_history") or [],
            board_snapshot=snapshot,
            policy_summary=runtime.policy.summary(),
            hint=state.get("handoff_hint", ""))

        log.info("supervisor_decision", next=decision.next_agent,
                 why=decision.why[:160], fallback=decision.fallback, hop=hops + 1)
        runtime.emit("supervisor", next=decision.next_agent, why=decision.why,
                     task=decision.task, fallback=decision.fallback, hop=hops + 1)

        await runtime.ctx.store.record(AuditEvent(
            run_id=state.get("run_id", ""), thread_id=state.get("thread_id", ""),
            environment=runtime.ctx.environment, step="supervisor",
            outcome=Outcome.OK,
            message=f"next={decision.next_agent}: {decision.why}"[:500],
            detail={"task": decision.task[:400], "fallback": decision.fallback,
                    "hop": hops + 1, "state": snapshot}))

        return PipelineState(next_agent=decision.next_agent, next_task=decision.task,
                             hops=hops + 1, handoff_hint="",
                             board=runtime.board.to_state(),
                             policy=runtime.policy.summary())

    return supervisor


def make_agent_node(runtime: AgenticRuntime):
    async def run_agent(state: PipelineState) -> PipelineState:
        runtime.sync_from(state)
        name = state.get("next_agent", "")
        agent = ROSTER.get(name)
        if agent is None:
            return {**halt(f"supervisor chose an unknown agent: {name!r}")}

        runner = AgentRunner(
            llm=runtime.llm, registry=runtime.registry, policy=runtime.policy,
            store=runtime.ctx.store, run_id=state.get("run_id", ""),
            thread_id=state.get("thread_id", ""),
            environment=runtime.ctx.environment, on_event=runtime.on_event,
            redact=getattr(runtime.ctx.settings, "redact_for_model", True))

        subjects = sorted(runtime.board.users) + sorted(runtime.board.work_items)
        brief = await runtime.memory.brief(subjects, tags=[name])
        context = context_for(runtime.board, brief)

        # results is an append-only channel: return only what this hop added,
        # or every earlier result is counted again on each hop.
        results_before = len(runtime.board.results)
        result = await runner.run(agent, state.get("next_task", ""), context)
        log.info("agent_finished", agent=name, iterations=result.iterations,
                 calls=len(result.calls), handoff=result.handoff_to,
                 stopped=result.stopped_because)

        entry = {
            "agent": name,
            "task": state.get("next_task", "")[:200],
            "output": result.output[:600],
            "calls": len(result.calls),
            "denials": sum(1 for c in result.calls if c.denied),
            "stopped": result.stopped_because,
            "handoff_to": result.handoff_to,
            "tools": summarise_calls(result.calls)[:1200],
        }

        await runtime.ctx.store.record(AuditEvent(
            run_id=state.get("run_id", ""), thread_id=state.get("thread_id", ""),
            environment=runtime.ctx.environment, step=f"agent:{name}",
            outcome=Outcome.OK if result.finished or result.handoff_to
            else Outcome.SKIPPED,
            message=result.output[:500], detail=entry))

        if result.stopped_because.startswith("model unavailable") and not result.calls:
            # The supervisor would route to the next agent, which would fail the
            # same way. Stop once, with the reason, instead of spending every hop.
            return {**halt(f"the agent model is unavailable - {result.stopped_because}. "
                           "Fix it (check the key and model with --check), then run "
                           "again."),
                    "agent_history": [entry], "board": runtime.board.to_state()}

        handoff = honour_handoff(result)
        update = PipelineState(
            agent_history=[entry],
            board=runtime.board.to_state(),
            policy=runtime.policy.summary(),
            results=list(runtime.board.results[results_before:]),
            verified_userids=sorted(runtime.board.verified),
            statuses=dict(runtime.board.statuses),
            users=list(runtime.board.users.values()),
            evidence_paths=dict(runtime.board.evidence),
            handoff_hint=handoff.next_agent if handoff else "")
        return update

    return run_agent


def make_agentic_approval_node(runtime: AgenticRuntime):
    """The gate, entered when an agent decides the batch is ready for a human."""

    async def approval(state: PipelineState) -> PipelineState:
        runtime.sync_from(state)
        items = runtime.board.approval_items()
        if not items:
            runtime.board.approval_requested = False
            return PipelineState(board=runtime.board.to_state(),
                                 handoff_hint="",
                                 agent_history=[{
                                     "agent": "approval",
                                     "output": ("approval was requested but no user "
                                                "qualified; nothing to decide"),
                                     "calls": 0, "denials": 0,
                                     "stopped": "empty batch"}])

        settings = runtime.ctx.settings
        request = ApprovalRequest(
            thread_id=state["thread_id"], run_id=state["run_id"],
            environment=runtime.ctx.environment,
            expires_at=utcnow() + timedelta(minutes=settings.approval_ttl_minutes),
            plan_hash=plan_hash(items), items=items)
        # This node runs twice - once to pause, once on resume. Only the first
        # pass may save the request and send the card.
        request, is_new = await open_request(runtime.ctx.store, request)

        if is_new:
            if runtime.notifier is not None:
                try:
                    await runtime.notifier(request)
                except Exception as err:  # noqa: BLE001 - a card that fails to send
                    log.warning("approval_notification_failed", error=str(err))
            log.info("awaiting_approval", thread_id=request.thread_id,
                     users=request.user_count,
                     reason=runtime.board.approval_reason[:160])
            runtime.emit("approval_required", request=request,
                         reason=runtime.board.approval_reason)

        from langgraph.types import interrupt

        payload = interrupt({
            "type": "approval_required",
            "thread_id": request.thread_id,
            "plan_hash": request.plan_hash,
            "reason": runtime.board.approval_reason,
            "expires_at": request.expires_at.isoformat(),
            "items": [i.model_dump(mode="json") for i in items],
        })

        decision = _coerce(payload, request)
        if decision is None:
            return halt("the approval payload was not a valid decision")
        if decision.plan_hash and decision.plan_hash != request.plan_hash:
            return halt("the batch changed after it was approved; re-approval required")
        if utcnow() > request.expires_at:
            return halt(f"approval expired at {request.expires_at.isoformat()}")

        await runtime.ctx.store.save_approval_decision(decision)
        await runtime.ctx.store.record(AuditEvent(
            run_id=state["run_id"], thread_id=state["thread_id"],
            environment=runtime.ctx.environment, step="approval",
            outcome=Outcome.OK if decision.approved else Outcome.SKIPPED,
            approver=decision.approver,
            message="approved" if decision.approved else "rejected",
            detail={"plan_hash": request.plan_hash,
                    "users": [i.userid for i in items],
                    "comment": decision.comment}))

        runtime.policy.approval = decision
        runtime.policy.prod_confirmed = True
        runtime.ctx.approval = decision
        runtime.board.approval_requested = False

        if not decision.approved:
            return {**halt(f"rejected by {decision.approver}"),
                    "approval": decision, "approval_request": request,
                    "board": runtime.board.to_state()}

        return PipelineState(approval=decision, approval_request=request,
                             plan_hash=request.plan_hash,
                             board=runtime.board.to_state(),
                             policy=runtime.policy.summary(),
                             agent_history=[{
                                 "agent": "approval", "calls": 0, "denials": 0,
                                 "output": (f"approved by {decision.approver} for "
                                            f"{len(decision.approved_userids) or len(items)} "
                                            "user(s); writes are now permitted"),
                                 "stopped": "approved"}])

    return approval


def _coerce(payload, request: ApprovalRequest) -> ApprovalDecision | None:
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


def make_agentic_auditor_node(runtime: AgenticRuntime):
    async def auditor(state: PipelineState) -> PipelineState:
        report = summarise(state)
        report["agents"] = [entry.get("agent") for entry in
                            (state.get("agent_history") or [])]
        report["hops"] = state.get("hops", 0)
        report["policy"] = runtime.policy.summary()
        report["orchestration"] = "agentic"

        await runtime.ctx.store.record(AuditEvent(
            run_id=state.get("run_id", ""), thread_id=state.get("thread_id", ""),
            environment=runtime.ctx.environment, step="run_summary",
            outcome=Outcome.FAILED if report["by_outcome"].get("failed") else Outcome.OK,
            approver=(state.get("approval").approver if state.get("approval") else ""),
            message=("run halted: " + report["halt_reason"]) if report["halted"]
            else "run complete",
            detail=report))
        log.info("agentic_run_summary", hops=report["hops"],
                 agents=report["agents"], writes=report["policy"]["writes"],
                 denials=report["policy"]["denials"])
        return PipelineState()

    return auditor


# ------------------------------------------------------------------- assembly

def needs_approval(state: PipelineState) -> bool:
    """Whether the gate must open before anything else happens.

    Either an agent asked for a human and nobody has decided, or the run now
    holds users the existing approval never showed to anyone.
    """
    board = state.get("board") or {}
    approval = state.get("approval")
    if approval is None:
        return bool(board.get("approval_requested"))
    approved = {u.upper() for u in (getattr(approval, "approved_userids", None) or [])}
    if not approved or not getattr(approval, "approved", False):
        return False  # a legacy decision, or a rejection that already halted the run
    return bool({u.upper() for u in (board.get("users") or {})} - approved)


def route_from_supervisor(state: PipelineState) -> str:
    """Where the supervisor's decision actually sends the run."""
    if state.get("halted"):
        return "auditor"
    chosen = (state.get("next_agent") or "").upper()
    if chosen == "DONE" or not state.get("next_agent"):
        return "auditor"
    # An agent asked for a human, and none has decided yet: the gate takes
    # priority over whatever the supervisor picked, because no write may
    # proceed without it anyway.
    if needs_approval(state):
        return "approval"
    return "agent"


def route_from_agent(state: PipelineState) -> str:
    if state.get("halted"):
        return "auditor"
    if needs_approval(state):
        return "approval"
    return "supervisor"


def route_from_approval(state: PipelineState) -> str:
    return "auditor" if state.get("halted") else "supervisor"


def build_agentic_graph(runtime: AgenticRuntime, *, checkpointer=None):
    """Compile the supervisor-and-agents graph."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(PipelineState)
    builder.add_node("supervisor", make_supervisor_node(runtime))
    builder.add_node("agent", make_agent_node(runtime))
    builder.add_node("approval", make_agentic_approval_node(runtime))
    builder.add_node("auditor", make_agentic_auditor_node(runtime))

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", route_from_supervisor,
                                  {"agent": "agent", "approval": "approval",
                                   "auditor": "auditor"})
    builder.add_conditional_edges("agent", route_from_agent,
                                  {"supervisor": "supervisor", "approval": "approval",
                                   "auditor": "auditor"})
    builder.add_conditional_edges("approval", route_from_approval,
                                  {"supervisor": "supervisor", "auditor": "auditor"})
    builder.add_edge("auditor", END)

    return builder.compile(checkpointer=checkpointer)
