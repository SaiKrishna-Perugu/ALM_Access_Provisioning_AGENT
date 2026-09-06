"""The agent runtime: a real tool-calling loop with a hard boundary around it.

An agent here is a model that decides its own next action. It reads a task,
chooses tools, sees the results, and keeps going until it decides it is done,
hands off to another agent, or runs out of budget. Nothing about the sequence is
predetermined - that is what makes this agentic rather than a workflow with an
LLM in it.

What *is* predetermined is what a tool call is permitted to do. Every call goes
through :class:`~alm_agents.policy.PolicyEngine` first, and a denial comes back
to the model as an observation rather than an exception. That distinction is the
whole design: an agent told "you may not write yet, call request_human_approval
first" can adapt and do the right thing, which is the behaviour we want. An agent
that could ignore the denial would be a liability.

Four bounds keep a reasoning loop from becoming an incident:

* iterations per agent run
* tool calls per run (shared across all agents, enforced by the policy engine)
* wall clock per agent run
* an explicit terminal action - ``finish`` or ``handoff`` - so "done" is a
  decision the agent states, not something inferred from silence
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from alm_core.logging import get_logger
from alm_core.models import AuditEvent, Outcome

from .policy import PolicyEngine

log = get_logger("alm.agent")

DEFAULT_MAX_ITERATIONS = 12
DEFAULT_TIMEOUT_SECONDS = 300
MAX_OBSERVATION_CHARS = 6000


@dataclass
class ToolSpec:
    """One capability an agent may invoke."""

    name: str
    description: str
    args_schema: Any                       # pydantic BaseModel subclass
    run: Callable[..., Awaitable[str]]     # returns the observation text
    is_write: bool = False
    terminal: bool = False                 # ends the agent's loop when called


class ToolRegistry:
    """The tools available in this process, and how to expose them to a model."""

    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs: dict[str, ToolSpec] = {s.name: s for s in (specs or [])}

    def add(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def subset(self, names: list[str]) -> list[ToolSpec]:
        missing = [n for n in names if n not in self._specs]
        if missing:
            raise KeyError(f"unknown tools requested: {', '.join(missing)}")
        return [self._specs[n] for n in names]

    def as_openai_schema(self, names: list[str]) -> list[dict]:
        """Tool definitions in the shape a chat model expects."""
        schemas = []
        for spec in self.subset(names):
            schemas.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.args_schema.model_json_schema(),
                },
            })
        return schemas


@dataclass
class ToolCall:
    name: str
    args: dict
    observation: str = ""
    denied: bool = False
    ms: int = 0


@dataclass
class AgentResult:
    """What an agent produced, and how it got there."""

    agent: str
    output: str = ""
    handoff_to: str = ""
    handoff_task: str = ""
    finished: bool = False
    calls: list[ToolCall] = field(default_factory=list)
    iterations: int = 0
    stopped_because: str = ""
    structured: dict = field(default_factory=dict)

    @property
    def wrote_anything(self) -> bool:
        return any(c.name in {"provision_jts_user", "reactivate_jts_user",
                              "request_ad_group_membership", "post_workitem_comment",
                              "attach_workitem_evidence"} and not c.denied
                   for c in self.calls)

    def transcript(self, limit: int = 20) -> list[dict]:
        return [{"tool": c.name, "args": c.args, "denied": c.denied,
                 "observation": c.observation[:400]} for c in self.calls[-limit:]]


@dataclass
class Agent:
    """A named role with a system prompt, a toolset and a model."""

    name: str
    role: str
    system_prompt: str
    tools: list[str]
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    temperature: float = 0.0

    def instructions(self, registry: ToolRegistry) -> str:
        """The system prompt plus the operating rules every agent shares."""
        available = "\n".join(
            f"- {spec.name}: {spec.description.splitlines()[0]}"
            for spec in registry.subset(self.tools))
        return f"""{self.system_prompt}

You are the {self.name} agent. {self.role}

Your tools:
{available}

How you must operate:
- Work by calling tools. Do not describe what you would do - do it.
- Never invent a user ID, an e-mail address, a work item number or a status. If
  you do not know something, call a tool to find out, or say you do not know.
- A tool result beginning with DENIED is a policy decision. Do not retry it
  unchanged and do not attempt to work around it. Read the reason and choose a
  legal action instead.
- A tool result beginning with ERROR is a failure you may reason about. Retry
  differently at most once, then report it.
- When your part is done, call finish with a short factual summary.
- If the work belongs to another agent, call handoff with a specific task for
  them. Do not hand off a vague instruction.
- Prefer fewer, better tool calls. You have a shared budget with the other
  agents in this run."""


class AgentRunner:
    """Executes an agent's loop against a model and a tool registry."""

    def __init__(self, *, llm, registry: ToolRegistry, policy: PolicyEngine,
                 store=None, run_id: str = "", thread_id: str = "",
                 environment: str = ""):
        self.llm = llm
        self.registry = registry
        self.policy = policy
        self.store = store
        self.run_id = run_id
        self.thread_id = thread_id
        self.environment = environment

    # ------------------------------------------------------------ execution

    async def _execute(self, agent: Agent, name: str, args: dict) -> tuple[str, bool]:
        """Run one tool call. Returns ``(observation, denied)``."""
        spec = self.registry.get(name)
        if spec is None:
            return (f"ERROR: no tool named {name!r}. Available: "
                    f"{', '.join(agent.tools)}"), False

        if name not in agent.tools:
            return (f"DENIED: the {agent.name} agent may not call {name}. "
                    f"Hand off to an agent that can."), True

        verdict = self.policy.check(name, args)
        if not verdict:
            await self._record(agent, name, args, Outcome.SKIPPED, verdict.reason,
                               denied=True)
            return f"DENIED: {verdict.reason}", True

        self.policy.note_tool_call()
        try:
            validated = spec.args_schema.model_validate(args)
        except Exception as err:  # pydantic ValidationError
            return (f"ERROR: your arguments did not match the tool's schema - {err}. "
                    "Read the schema and call it again correctly."), False

        try:
            observation = await spec.run(**validated.model_dump())
        except Exception as err:  # noqa: BLE001 - a tool failure is the agent's to handle
            log.exception("tool_failed", tool=name, agent=agent.name)
            await self._record(agent, name, args, Outcome.FAILED,
                               f"{type(err).__name__}: {err}")
            return f"ERROR: {type(err).__name__}: {err}", False

        if spec.is_write:
            self.policy.note_write()
        return str(observation)[:MAX_OBSERVATION_CHARS], False

    async def _record(self, agent: Agent, tool: str, args: dict, outcome: Outcome,
                      message: str, denied: bool = False) -> None:
        """Every tool call an agent makes is auditable, including refusals."""
        if self.store is None:
            return
        try:
            await self.store.record(AuditEvent(
                run_id=self.run_id, thread_id=self.thread_id,
                environment=self.environment, step=f"agent:{agent.name}",
                userid=str(args.get("userid") or ""),
                work_item_id=str(args.get("work_item_id") or ""),
                outcome=outcome, message=message[:500],
                detail={"tool": tool, "denied": denied,
                        "args": {k: v for k, v in args.items() if k != "text"}}))
        except Exception:  # noqa: BLE001 - auditing must not break the run
            log.exception("agent_audit_write_failed", tool=tool)

    # ----------------------------------------------------------------- loop

    async def run(self, agent: Agent, task: str, context: str = "") -> AgentResult:
        """Run one agent until it finishes, hands off, or exhausts its budget."""
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

        result = AgentResult(agent=agent.name)
        deadline = time.monotonic() + agent.timeout_seconds
        model = self.llm.bind_tools(self.registry.as_openai_schema(agent.tools))

        messages: list[Any] = [
            SystemMessage(content=agent.instructions(self.registry)),
            HumanMessage(content=f"{task}\n\n{context}".strip()),
        ]

        for iteration in range(1, agent.max_iterations + 1):
            result.iterations = iteration
            if time.monotonic() > deadline:
                result.stopped_because = "timeout"
                break

            try:
                response: AIMessage = await asyncio.wait_for(
                    model.ainvoke(messages), timeout=max(5, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                result.stopped_because = "model call timed out"
                break
            except Exception as err:  # noqa: BLE001 - a model outage is not a crash
                log.warning("model_call_failed", agent=agent.name, error=str(err))
                result.stopped_because = f"model unavailable: {err}"
                break

            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []

            if not tool_calls:
                # No tool call and no explicit finish: treat the text as the
                # answer but say so, because an agent that stops without
                # deciding is a weaker signal than one that calls finish.
                result.output = _text(response)
                result.stopped_because = "model answered without calling finish"
                break

            for call in tool_calls:
                name = call.get("name", "")
                args = call.get("args", {}) or {}
                started = time.monotonic()
                observation, denied = await self._execute(agent, name, args)
                record = ToolCall(name=name, args=args, observation=observation,
                                  denied=denied,
                                  ms=int((time.monotonic() - started) * 1000))
                result.calls.append(record)
                messages.append(ToolMessage(content=observation,
                                            tool_call_id=call.get("id", name)))
                log.info("agent_tool_call", agent=agent.name, tool=name,
                         denied=denied, ms=record.ms)

                spec = self.registry.get(name)
                if spec is not None and spec.terminal and not denied:
                    if name == "handoff":
                        result.handoff_to = str(args.get("to", ""))
                        result.handoff_task = str(args.get("task", ""))
                        result.output = observation
                    else:
                        result.finished = True
                        result.output = str(args.get("summary", observation))
                        result.structured = {k: v for k, v in args.items()
                                             if k != "summary"}
                    result.stopped_because = name
                    return result
        else:
            result.stopped_because = "iteration limit"

        if not result.output:
            result.output = f"({agent.name} stopped: {result.stopped_because})"
        return result


def _text(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content
                        if isinstance(part, dict))
    return str(content)


def summarise_calls(calls: list[ToolCall]) -> str:
    """A compact record of what an agent did, for the next agent's context."""
    if not calls:
        return "(no tool calls)"
    lines = []
    for call in calls:
        status = "DENIED" if call.denied else "ok"
        detail = json.dumps(call.args, default=str)[:160]
        lines.append(f"- {call.name}({detail}) -> {status}")
    return "\n".join(lines)
