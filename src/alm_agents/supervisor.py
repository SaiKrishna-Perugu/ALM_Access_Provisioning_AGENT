"""The supervisor: a model that decides which agent acts next.

This is what makes the system multi-agent rather than a pipeline with agents in
it. There is no fixed edge from validator to provisioner. The supervisor reads
what has been established, what each agent reported, and what the policy engine
has refused, and chooses - which means it can send work back to the extractor
when the validator finds nothing to validate, skip evidence when nobody
verified, or call the remediator out of order when something breaks.

Three properties keep an autonomous router from becoming an unbounded one:

* **A deterministic fallback.** If the model is unavailable, returns an unknown
  agent, or produces nonsense, routing falls back to the nominal sequence. The
  run degrades to the workflow behaviour rather than stopping.
* **A hop budget.** Every routing decision costs a hop; the run ends when they
  run out. A supervisor that oscillates between two agents cannot do so forever.
* **No authority of its own.** The supervisor has no tools. It cannot write, it
  cannot approve, and it cannot grant an agent a capability the roster did not
  give it. It chooses the order of work, nothing else.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from alm_core.logging import get_logger, scrub_secrets

from .agent import AgentResult, _text
from .roster import NOMINAL_SEQUENCE, ROSTER, describe_roster

log = get_logger("alm.supervisor")

MAX_HOPS = 24

SUPERVISOR_PROMPT = """You coordinate a team of agents that provisions ALM
platform access. You do not do the work yourself and you have no tools; you
decide who acts next and what exactly they should do.

The team:
{roster}

The usual order is: {sequence}. Depart from it when the situation warrants -
that judgement is why you exist rather than a fixed pipeline:

- If an agent found nothing to work on, do not send the next agent in line to
  work on nothing. End the run, or send the work back a step.
- If something failed, consider the remediator before continuing.
- If nobody was verified, there is nothing to attach or comment on. Do not send
  the evidence_officer or the closer to produce reports about users who do not
  have access.
- Every verified user gets a profile screenshot attached to their work item
  before the closer comments - including a user who already had access, where
  the screenshot is the proof for this request. So after approval the order is
  verifier (if anyone is unverified), then evidence_officer, then closer.
- If a policy denial is blocking progress, read the reason. If it says approval
  is required, the risk_officer requests it - nobody else can, and no agent can
  route around it.
- A write DENIED because shadow mode is on is the expected result of a dry
  run, not a failure. Do not send the remediator for it. Once the plan is
  complete, the run is DONE.
- Never send an agent to redo work another has already completed successfully.

Reply with JSON only, no prose around it:
{{"next": "<agent name, or DONE>", "task": "<specific instruction>",
  "why": "<one sentence>"}}

"task" must be an instruction that agent can act on immediately, naming the
users or work items concerned. Never "continue" or "proceed"."""


@dataclass
class Decision:
    next_agent: str
    task: str
    why: str = ""
    fallback: bool = False

    @property
    def done(self) -> bool:
        return self.next_agent.upper() == "DONE"


def _history_digest(history: list[dict], limit: int = 8) -> str:
    if not history:
        return "(nothing has run yet)"
    lines = []
    for entry in history[-limit:]:
        lines.append(
            f"- {entry.get('agent')}: {entry.get('output', '')[:280]} "
            f"[{entry.get('calls', 0)} tool call(s), "
            f"{entry.get('denials', 0)} denied, stopped: {entry.get('stopped', '?')}]")
    return "\n".join(lines)


def deterministic_next(history: list[dict], board_snapshot: dict) -> Decision:
    """The fallback route: the nominal sequence, skipping what cannot apply.

    Used when the model is unavailable or unusable. It is the old workflow, so a
    model outage degrades this system to the behaviour it had before rather than
    halting it.
    """
    done = {entry.get("agent") for entry in history}
    for name in NOMINAL_SEQUENCE:
        if name in done:
            continue
        if name in ("evidence_officer", "closer") and not board_snapshot.get("verified"):
            continue
        if name == "provisioner" and not board_snapshot.get("approved"):
            continue
        return Decision(next_agent=name,
                        task=f"Perform your part for this run: {name}.",
                        why="deterministic fallback route", fallback=True)
    return Decision(next_agent="DONE", task="", why="nominal sequence complete",
                    fallback=True)


async def decide(llm, *, history: list[dict], board_snapshot: dict,
                 policy_summary: dict, hint: str = "") -> Decision:
    """Ask the supervisor model who should act next; fall back if it cannot."""
    fallback = deterministic_next(history, board_snapshot)
    if llm is None:
        return fallback

    prompt = SUPERVISOR_PROMPT.format(roster=describe_roster(),
                                      sequence=" -> ".join(NOMINAL_SEQUENCE))
    context = f"""What has run so far:
{_history_digest(history)}

Current state:
{json.dumps(board_snapshot, indent=2, default=str)}

Policy so far: {json.dumps(policy_summary, default=str)}
{f'An agent asked to hand off to: {hint}' if hint else ''}

Who acts next?"""

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = await llm.ainvoke([SystemMessage(content=prompt),
                                      HumanMessage(content=context)])
        # Gemini may answer with a list of content parts, not a string.
        text = _text(response) or ""
    except Exception as err:  # noqa: BLE001 - a model outage must not stop the run
        log.warning("supervisor_model_failed", error=scrub_secrets(str(err))[:300])
        return fallback

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        log.warning("supervisor_unparseable", reply=text[:200])
        return fallback
    try:
        payload = json.loads(text[start:end + 1])
    except ValueError:
        log.warning("supervisor_invalid_json", reply=text[:200])
        return fallback

    chosen = str(payload.get("next", "")).strip()
    if chosen.upper() == "DONE":
        return Decision("DONE", "", str(payload.get("why", "")))
    if chosen not in ROSTER:
        log.warning("supervisor_chose_unknown_agent", chosen=chosen)
        return fallback

    task = str(payload.get("task", "")).strip()
    if not task or task.lower() in {"continue", "proceed", "carry on"}:
        task = f"Perform your part for this run: {chosen}."
    return Decision(chosen, task, str(payload.get("why", "")))


def honour_handoff(result: AgentResult) -> Decision | None:
    """Accept an agent's explicit handoff, when it names a real agent.

    Agent-to-agent handoff is direct: the agent that just did the work knows
    best who should continue. The supervisor still sees it as a hint on the next
    hop, and a handoff to a nonexistent agent falls back to the supervisor
    rather than ending the run.
    """
    if not result.handoff_to:
        return None
    target = result.handoff_to.strip()
    if target not in ROSTER:
        log.warning("handoff_to_unknown_agent", agent=result.agent, target=target)
        return None
    return Decision(target, result.handoff_task or
                    f"Continue from {result.agent}: {result.output[:200]}",
                    why=f"handoff from {result.agent}")
