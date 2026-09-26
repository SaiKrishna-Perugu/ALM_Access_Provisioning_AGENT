"""Run the multi-agent system locally against the real EWM, JTS and GPT.

    python src/agent_local.py --check
    python src/agent_local.py --work-item <id>            # dry run
    python src/agent_local.py --work-item <id> --commit   # writes, after your approval

A thin launcher so this runs like every other script in src/. The
implementation is in alm_agents/local.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alm_agents.local import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
