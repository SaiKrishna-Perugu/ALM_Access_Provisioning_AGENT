"""Grade the agents on fixed scenarios and on recorded TEST runs, with no VPN.

    python src/agent_eval.py --list
    python src/agent_eval.py                                  # every built-in scenario
    python src/agent_eval.py --scenario standard --scenario dry_run
    python src/agent_eval.py --recorded out/evals/recorded    # replay recorded TEST runs

A thin launcher so the eval runs like every other script in src/. The
scenarios, the checks and the recorder are in alm_agents/evals.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alm_agents.evals import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
