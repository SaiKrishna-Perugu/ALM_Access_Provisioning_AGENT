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

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

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


# Every alm_core type that lives in PipelineState, and so in a checkpoint.
# LangGraph deserialises unlisted types with a warning today and will refuse
# them in a future release - which would strand every run parked at the
# approval gate across that upgrade. Add a type here when it enters the state.
CHECKPOINT_TYPES = (
    "SourceWorkItem", "RequestedUser", "WorkItem", "UserStatus", "UserState",
    "RiskLevel", "Operation", "Outcome", "ProvisionResult", "ApprovalItem",
    "ApprovalRequest", "ApprovalDecision",
)


def checkpoint_serde():
    """The checkpoint serializer, with this system's types explicitly allowed."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(
        allowed_msgpack_modules=[("alm_core.models", name) for name in CHECKPOINT_TYPES])


def checkpoint_db_path(ledger_path: str) -> str:
    """Checkpoints live in their own file beside the ledger: LangGraph manages
    its schema, and keeping it apart keeps the ledger's tables ours alone."""
    root, _ext = os.path.splitext(ledger_path)
    return f"{root}-checkpoints.db"


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

        async with AsyncPostgresSaver.from_conn_string(
                postgres_dsn(settings), serde=checkpoint_serde()) as saver:
            await saver.setup()
            yield saver
        return

    if getattr(settings, "ledger_path", ""):
        # Local mode: checkpoints next to the ledger, so a paused or crashed run
        # resumes from the same file.
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as err:  # pragma: no cover
            raise ConfigError(
                "langgraph-checkpoint-sqlite is required for local durable runs") from err
        path = checkpoint_db_path(settings.ledger_path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        async with aiosqlite.connect(path) as conn:
            saver = AsyncSqliteSaver(conn, serde=checkpoint_serde())
            await saver.setup()
            yield saver
        return

    if not settings.shadow_mode:
        raise ConfigError(
            "durable checkpointing requires ALM_POSTGRES_DSN or ALM_LEDGER_PATH: without "
            "it a run paused for approval would not survive a restart")
    from langgraph.checkpoint.memory import MemorySaver

    log.warning("using_memory_checkpointer", reason="shadow mode, no postgres dsn")
    yield MemorySaver(serde=checkpoint_serde())


@dataclass
class Services:
    """What every run in a process shares: connections, clients, the store.

    Nothing here belongs to one run. A run's own state - its tool context, its
    blackboard, its policy budgets and its approval - is built fresh by
    :func:`run_session`, so concurrent runs in one process cannot see each
    other's users, approvals or counters.
    """

    settings: object
    store: object
    client: object                 # alm_core.auth.JazzClient
    checkpointer: object
    agent_llm: object = None
    supervisor_llm: object = None
    notifier: object = None
    skip_ad: bool = False
    # One bound on concurrent writes for the whole process, not per run: EWM
    # and JTS see the sum of all runs.
    write_limit: asyncio.Semaphore | None = field(default=None, repr=False)

    @property
    def agentic(self) -> bool:
        return getattr(self.settings, "orchestration", "") in ("agentic", "guided")


@asynccontextmanager
async def build_services(settings=None, *, notifier=None, skip_ad: bool = False):
    """Open everything runs share, and close it afterwards. Yields :class:`Services`.

    Which graph the runs get depends on ``ALM_ORCHESTRATION``:

    * ``agentic`` / ``guided`` (default) - an LLM supervisor routes autonomous,
      tool-calling agents.
    * ``deterministic`` - the fixed node sequence. Same tools, same guarantees,
      no routing model.

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

    try:
        async with checkpointer_for(settings) as checkpointer:
            services = Services(settings=settings, store=store, client=client,
                                checkpointer=checkpointer, notifier=notifier,
                                skip_ad=skip_ad,
                                write_limit=asyncio.Semaphore(
                                    getattr(settings, "max_concurrent_writes", 4)))
            if services.agentic:
                from . import llm as llm_module
                from .memory import MemoryStore

                services.agent_llm = llm_module.get_agent_llm(settings)
                if services.agent_llm is None:
                    raise ConfigError(
                        "agentic orchestration was requested but no model client could "
                        f"be built (ALM_LLM_PROVIDER={settings.llm_provider}). For "
                        "gemini_api, set GEMINI_API_KEY or the Secret Manager secret "
                        "named by ALM_GEMINI_API_KEY_SECRET_NAME. For vertex, check "
                        "GOOGLE_CLOUD_PROJECT, ALM_REGION and that the runtime service "
                        "account holds roles/aiplatform.user. Or set "
                        "ALM_ORCHESTRATION=deterministic.")
                services.supervisor_llm = llm_module.get_supervisor_llm(settings)
                await MemoryStore(store).migrate()
                log.info("orchestration_selected", mode=settings.orchestration,
                         agents=len(ROSTER), max_hops=settings.max_hops)
            else:
                log.info("orchestration_selected", mode="deterministic")
            yield services
    finally:
        client.close()
        await store.close()


def run_session(services: Services, *, control=None, on_event=None, backend=None,
                shots_dir: str = "", mode: str = ""):
    """A graph and tool context for exactly one run. Returns ``(graph, ctx)``.

    Cheap to call: it builds in-memory objects and compiles the graph, and
    reuses the shared connections in ``services``. Call it once per start and
    once per resume - a resumed run rehydrates everything it needs from its
    checkpoint.

    ``mode`` is ``"dry"`` or ``"commit"`` for this run; empty means the
    deployment's default. A deployment configured not to write
    (``ALM_SHADOW_MODE=true``) refuses ``"commit"``.
    """
    settings = services.settings
    if mode not in ("", "dry", "commit"):
        raise ConfigError(f"unknown run mode {mode!r}: use dry or commit")
    if mode == "commit" and settings.shadow_mode:
        raise ConfigError("this deployment does not write (ALM_SHADOW_MODE=true); "
                          "a writing run is refused")
    if mode == "dry" and not settings.shadow_mode:
        settings = settings.model_copy(update={"shadow_mode": True})
    ctx = ToolContext(settings=settings, client=services.client,
                      store=services.store, run_id="", semaphore=services.write_limit)
    if not services.agentic:
        return build_graph(ctx, checkpointer=services.checkpointer,
                           notifier=services.notifier, skip_ad=services.skip_ad), ctx

    from .agentic import AgenticRuntime, build_agentic_graph
    from .memory import MemoryStore

    runtime = AgenticRuntime(
        ctx, llm=services.agent_llm, supervisor_llm=services.supervisor_llm,
        memory=MemoryStore(services.store), max_hops=services.settings.max_hops,
        notifier=services.notifier, backend=backend, on_event=on_event,
        control=control, shots_dir=shots_dir)
    return build_agentic_graph(runtime, checkpointer=services.checkpointer), ctx


@asynccontextmanager
async def build_runtime(settings=None, *, notifier=None, skip_ad: bool = False):
    """One run's ``(graph, ctx)`` with its services - for scripts and smoke tests.

    A long-lived process that serves many runs (the API, a worker) must use
    :func:`build_services` once and :func:`run_session` per run instead: the
    graph and context yielded here belong to a single run.
    """
    async with build_services(settings, notifier=notifier, skip_ad=skip_ad) as services:
        yield run_session(services)


def run_config(thread_id: str) -> dict:
    """LangGraph invocation config. The thread id is the approval's identity."""
    return {"configurable": {"thread_id": thread_id}}


def run_mode_of(ctx: ToolContext) -> str:
    """The mode a process runs in: "dry-run" (shadow) or "commit"."""
    return "dry-run" if ctx.shadow else "commit"


async def start_run(graph, ctx: ToolContext, *, thread_id: str,
                    work_item_ids: list[str] | None = None,
                    trigger: str = "manual", operator_request: str = "") -> dict:
    """Begin a run. Returns the graph result, which may be an interrupt."""
    run_id = new_run_id()
    ctx.run_id = run_id
    ctx.thread_id = thread_id
    ctx.approval = None

    from alm_core.logging import bind_run

    bind_run(run_id=run_id, thread_id=thread_id)
    state = new_state(run_id, thread_id, ctx.environment, trigger, work_item_ids,
                      run_mode=run_mode_of(ctx), operator_request=operator_request)
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
