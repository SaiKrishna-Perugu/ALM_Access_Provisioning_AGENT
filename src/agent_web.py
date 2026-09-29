"""The ALM agents' web console, on this machine only.

    python src/agent_web.py              # real EWM/JTS: asks the Jazz password once, here
    python src/agent_web.py --sandbox    # simulated estate, real model: works off the VPN

Prints a one-time link; open it to sign the browser in. A thin launcher so
the console runs like every other script in src/. The server and its
safeguards are in alm_agents/web.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alm_agents.web import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
