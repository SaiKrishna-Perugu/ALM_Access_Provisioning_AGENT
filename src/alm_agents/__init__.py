"""The multi-agent layer: a supervisor, nine agents, and the guards around them.

Two orchestrations, selected by ``ALM_ORCHESTRATION``:

* **agentic** (default) - :mod:`alm_agents.agentic`. An LLM supervisor chooses
  which agent acts next; each agent runs its own tool-calling loop and decides
  its own actions; agents hand off to each other directly. The sequence differs
  between runs.
* **deterministic** - :mod:`alm_agents.graph`. The same tools and the same
  guarantees on a fixed sequence. This is the fallback when the model is
  unavailable, and the mode to use when a run must be reproducible.

Agency is real here: the agents decide. What they cannot do is decide their way
past :mod:`alm_agents.policy`, which every tool call is checked against - a
denial comes back as an observation the agent must work with, not an exception
it can ignore. Safety lives in the tool boundary, not in the prompt.
"""
from .agent import Agent, AgentResult, AgentRunner, ToolRegistry, ToolSpec
from .graph import build_graph, build_runtime, resume_run, run_config, start_run
from .memory import MemoryStore
from .policy import PolicyEngine
from .roster import NOMINAL_SEQUENCE, ROSTER
from .state import PipelineState, new_state
from .toolkit import Blackboard, build_registry

__all__ = [
    # runtime
    "build_runtime", "start_run", "resume_run", "run_config", "build_graph",
    # agent machinery
    "Agent", "AgentRunner", "AgentResult", "ToolRegistry", "ToolSpec",
    "ROSTER", "NOMINAL_SEQUENCE",
    # supporting state
    "Blackboard", "build_registry", "MemoryStore", "PolicyEngine",
    "PipelineState", "new_state",
]
