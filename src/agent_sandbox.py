"""Run the multi-agent system with Gemini against a simulated ALM estate.

    python src/agent_sandbox.py --check
    python src/agent_sandbox.py

A thin launcher so the sandbox runs like every other script in src/. The
implementation, and what is and is not simulated, is in alm_agents/sandbox.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alm_agents.sandbox import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
