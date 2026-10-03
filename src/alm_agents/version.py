"""A fingerprint of everything that decides what the agents do.

The prompts, the roster (who may call which tool), the tool descriptions and
the models. Recorded on every run and every eval result, so a change in
behaviour can be traced to the change that caused it, and an eval result is
known to belong to the code that was graded.
"""
from __future__ import annotations

import hashlib
import json


def prompt_version(settings=None) -> str:
    from .roster import ROSTER
    from .supervisor import SUPERVISOR_PROMPT

    parts = {
        "supervisor": SUPERVISOR_PROMPT,
        "agents": {name: {"prompt": agent.system_prompt, "tools": list(agent.tools)}
                   for name, agent in sorted(ROSTER.items())},
    }
    if settings is not None:
        parts["models"] = [getattr(settings, "llm_provider", ""),
                           getattr(settings, "agent_model", ""),
                           getattr(settings, "supervisor_model", ""),
                           getattr(settings, "orchestration", "")]
    blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def run_version(settings, orchestration: str = "") -> str:
    """What a run records as its version: ``<orchestration>-<fingerprint>``.

    The orchestration part is also how a resumed run gets the graph it started
    with, whatever the deployment's setting is by then.
    """
    orchestration = orchestration or getattr(settings, "orchestration", "") or "guided"
    if orchestration != getattr(settings, "orchestration", ""):
        settings = settings.model_copy(update={"orchestration": orchestration})
    return f"{orchestration}-{prompt_version(settings)}"


def orchestration_of(version: str) -> str:
    """The orchestration a recorded version names; empty if it names none."""
    head = (version or "").split("-", 1)[0]
    return head if head in ("agentic", "guided", "deterministic") else ""
