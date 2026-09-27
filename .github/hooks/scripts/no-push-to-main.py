#!/usr/bin/env python3
"""Pre-push hook: refuse a direct push to main; changes reach it through a pull request.

GitHub branch protection needs a paid plan for a private repository, so this
is the local stand-in. pre-commit passes the branch being pushed to in
PRE_COMMIT_REMOTE_BRANCH. For a deliberate exception, e.g. repairing main by
hand, set ALM_ALLOW_PUSH_TO_MAIN=1 for that one push.
"""
from __future__ import annotations

import os
import sys

PROTECTED = {"refs/heads/main", "refs/heads/master"}


def main() -> int:
    target = os.getenv("PRE_COMMIT_REMOTE_BRANCH", "")
    if target not in PROTECTED or os.getenv("ALM_ALLOW_PUSH_TO_MAIN") == "1":
        return 0
    print(f"no-push-to-main: {target} is protected. Push a branch and open a pull "
          "request instead:\n"
          "  git switch -c my-change && git push -u origin my-change\n"
          "  gh pr create --fill", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
