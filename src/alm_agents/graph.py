"""Run assembly, and the deterministic graph.

Two orchestrations share this module's runtime, tools and guarantees:

* **agentic** (``alm_agents.agentic``, the default) - an LLM supervisor routes
  autonomous tool-calling agents. The order of work is decided per run.
* **deterministic** (``build_graph`` below) - a fixed node sequence:

      intake -> extraction -> validation -> APPROVAL -> jts -> ad -> verify
                                                                      |
                                               auditor <- closure <- evidence

  Every edge is conditional on ``halted``, so a run that loses its approval,
  finds nothing to do, or fails validation stops at the next boundary and still
  reaches the auditor. There is no path that writes without passing through the
  approval node.

The deterministic graph is not a legacy path. It is what the system falls back
to when the LLM is unavailable, and what to run when an execution must be
exactly reproducible - during an incident, for instance.

The checkpointer is what makes the human approval affordable in either mode:
state is persisted to Postgres on every superstep, so a run can wait four hours
while the container restarts, and resume where it stopped.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from alm_core.auth import JazzClient
from alm_core.credentials import build_resolver
from alm_core.errors import ConfigError
from alm_core.logging import get_logger, new_run_id
from alm_core.store import get_store
from alm_core.tools.base import ToolContext

from .nodes import (
    make_ad_node,
    make_approval_node,
    make_auditor_node,
    make_closure_node,
    make_evidence_node,
    make_extraction_node,
    make_intake_node,
    make_jts_node,
    make_validation_node,
    make_verification_node,
)
from .roster import ROSTER
from .state import PipelineState, new_state

log = get_logger("alm.graph")

# The AD group and domain are properties of the estate, not of a run.
DEFAULT_GROUP = "GR_D-JazzUser-NA"
DEFAULT_DOMAIN = "INETPSA"


def _continue_or_halt(next_node: str):
    """Route to ``next_node`` unless the run has halted, in which case audit."""

    def route(state: PipelineState) -> str:
        return "auditor" if state.get("halted") else next_node

    return route


def _after_extraction(state: PipelineState) -> str:
    if state.get("halted"):
        return "auditor"
    if not state.get("users"):
        # Nothing parseable and nothing the fallback could recover. The work
        # items are left untouched for a human; inventing users is not an option.
        log.warning("no_users_extracted",
                    work_items=len(state.get("work_item_ids") or []),
                    unparsed_rows=len(state.get("unparsed_rows") or []))
        return "auditor"
    return "validation"


def _after_validation(state: PipelineState) -> str:
    if state.get("halted"):
        return "auditor"
    actionable = [u for u in (state.get("users") or [])
                  if u.userid not in set(state.get("blocked_userids") or [])]
    if not actionable:
        log.warning("all_users_blocked", blocked=state.get("blocked_userids"))
        return "auditor"
    return "approval"


def _after_approval(state: PipelineState) -> str:
    if state.get("halted") or state.get("approval") is None:
        return "auditor"
    return "jts_provision"


def build_graph(ctx: ToolContext, *, checkpointer=None, notifier=None,
                group: str = DEFAULT_GROUP, domain: str = DEFAULT_DOMAIN,
                skip_ad: bool = False, shots_dir: str = ""):
    """Compile the supervisor graph."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as err:  # pragma: no cover
        raise ConfigError(
            "langgraph is required to build the supervisor graph "
            "(pip install -r requirements-cloud.txt)") from err

    builder = StateGraph(PipelineState)

    builder.add_node("intake", make_intake_node(ctx))
    builder.add_node("extraction", make_extraction_node(ctx))
    builder.add_node("validation", make_validation_node(ctx))
    builder.add_node("approval", make_approval_node(ctx, notifier=notifier))
    builder.add_node("jts_provision", make_jts_node(ctx))
    builder.add_node("ad_provision", make_ad_node(ctx, group=group, domain=domain))
    builder.add_node("verification", make_verification_node(ctx))
    builder.add_node("evidence", make_evidence_node(ctx, shots_dir))
    builder.add_node("closure", make_closure_node(ctx))
    builder.add_node("auditor", make_auditor_node(ctx))

    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", _continue_or_halt("extraction"),
                                  {"extraction": "extraction", "auditor": "auditor"})
    builder.add_conditional_edges("extraction", _after_extraction,
                                  {"validation": "validation", "auditor": "auditor"})
    builder.add_conditional_edges("validation", _after_validation,
                                  {"approval": "approval", "auditor": "auditor"})
    builder.add_conditional_edges("approval", _after_approval,
                                  {"jts_provision": "jts_provision", "auditor": "auditor"})

    after_jts = "verification" if skip_ad else "ad_provision"
    builder.add_conditional_edges("jts_provision", _continue_or_halt(after_jts),
                                  {after_jts: after_jts, "auditor": "auditor"})
    if not skip_ad:
        builder.add_conditional_edges("ad_provision", _continue_or_halt("verification"),
                                      {"verification": "verification",
                                       "auditor": "auditor"})

    # Evidence and closure are strictly for VERIFIED users, and both are
    # no-ops when nobody verified - so the run still reaches the auditor and
    # reports the timeout instead of quietly ending.
    builder.add_conditional_edges("verification", _continue_or_halt("evidence"),
                                  {"evidence": "evidence", "auditor": "auditor"})
    builder.add_edge("evidence", "closure")
    builder.add_edge("closure", "auditor")
    builder.add_edge("auditor", END)

    return builder.compile(checkpointer=checkpointer)


@asynccontextmanager
async def checkpointer_for(settings):
    """A Postgres checkpointer, or an in-memory one in shadow mode.

    An in-memory checkpointer cannot survive a restart, so a run paused at the
    approval gate would be lost. That is tolerable only when no write is
    pending, which is exactly what shadow mode guarantees.
    """
    if settings.postgres_dsn:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as err:  # pragma: no cover
            raise ConfigError(
                "langgraph-checkpoint-postgres is required for durable runs") from err
        from alm_core.credentials import postgres_dsn

        async with AsyncPostgresSaver.from_conn_string(postgres_dsn(settings)) as saver:
            await saver.setup()
            yield saver
        return

    if not settings.shadow_mode:
        raise ConfigError(
            "durable checkpointing requires ALM_POSTGRES_DSN: without it a run "
            "paused for approval would not survive a restart")
    from langgraph.checkpoint.memory import MemorySaver

    log.warning("using_memory_checkpointer", reason="shadow mode, no postgres dsn")
    yield MemorySaver()


@asynccontextmanager
async def build_runtime(settings=None, *, notifier=None, skip_ad: bool = False):
    """Assemble everything a run needs and tear it down afterwards.

    Yields ``(graph, ctx)``. Which graph depends on ``ALM_ORCHESTRATION``:

    * ``agentic`` (default) - an LLM supervisor routes autonomous, tool-calling
      agents. The order of work is decided per run.
    * ``deterministic`` - the fixed node sequence. Same tools, same guarantees,
      no routing model. Useful when the LLM is unavailable or when a run must be
      exactly reproducible.

    Both share the tool layer, the policy guards, the ledger and the audit
    trail, so switching modes changes how the work is sequenced and nothing
    about what a write is allowed to do.
    """
    if settings is None:
        from alm_core.config import get_settings

        settings = get_settings()

    store = await get_store(settings)
    resolver = build_resolver(settings)
    client = JazzClient(settings, resolver)
    ctx = ToolContext(settings=settings, client=client, store=store, run_id="")

    try:
        async with checkpointer_for(settings) as checkpointer:
            if settings.orchestration == "agentic":
                from . import llm as llm_module
                from .agentic import AgenticRuntime, build_agentic_graph
                from .memory import MemoryStore

                agent_llm = llm_module.get_agent_llm(settings)
                if agent_llm is None:
                    raise ConfigError(
                        "agentic orchestration was requested but no Vertex AI client "
                        "could be built. Check GOOGLE_CLOUD_PROJECT, ALM_REGION and that "
                        "the runtime service account holds roles/aiplatform.user, or set "
                        "ALM_ORCHESTRATION=deterministic.")

                memory = MemoryStore(store)
                await memory.migrate()
                runtime = AgenticRuntime(
                    ctx, llm=agent_llm,
                    supervisor_llm=llm_module.get_supervisor_llm(settings),
                    memory=memory, max_hops=settings.max_hops, notifier=notifier)
                log.info("orchestration_selected", mode="agentic",
                         agents=len(ROSTER), max_hops=settings.max_hops)
                yield build_agentic_graph(runtime, checkpointer=checkpointer), ctx
            else:
                log.info("orchestration_selected", mode="deterministic")
                yield build_graph(ctx, checkpointer=checkpointer, notifier=notifier,
                                  skip_ad=skip_ad), ctx
    finally:
        client.close()
        await store.close()


def run_config(thread_id: str) -> dict:
    """LangGraph invocation config. The thread id is the approval's identity."""
    return {"configurable": {"thread_id": thread_id}}


async def start_run(graph, ctx: ToolContext, *, thread_id: str,
                    work_item_ids: list[str] | None = None,
                    trigger: str = "manual") -> dict:
    """Begin a run. Returns the graph result, which may be an interrupt."""
    run_id = new_run_id()
    ctx.run_id = run_id
    ctx.thread_id = thread_id
    ctx.approval = None

    from alm_core.logging import bind_run

    bind_run(run_id=run_id, thread_id=thread_id)
    state = new_state(run_id, thread_id, ctx.environment, trigger, work_item_ids)
    log.info("run_started", trigger=trigger, work_items=work_item_ids or "queue",
             shadow=ctx.shadow)
    return await graph.ainvoke(state, config=run_config(thread_id))


async def resume_run(graph, ctx: ToolContext, *, thread_id: str, decision) -> dict:
    """Resume a run paused at the approval gate with a human's decision."""
    try:
        from langgraph.types import Command
    except ImportError as err:  # pragma: no cover
        raise ConfigError("langgraph is required to resume a run") from err

    ctx.thread_id = thread_id
    ctx.approval = decision
    payload = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else decision
    log.info("run_resuming", thread_id=thread_id,
             approved=getattr(decision, "approved", None))
    return await graph.ainvoke(Command(resume=payload), config=run_config(thread_id))
