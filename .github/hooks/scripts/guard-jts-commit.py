#!/usr/bin/env python3
"""PreToolUse guard: refuse an unsafe ``--commit`` run of any write step.

The original guard matched ``jts_import_users`` only. Five entry points can
write -- and the documented primary one, ``run_pipeline.py --commit``, was
completely unguarded, as were the GPT AD-group step and both work-item writers.
This keys on ``--commit`` for every write script instead.

Checks applied to a commit run:

  jts_import_users / elm_gpt / ewm_comment_workitems / jts_profile_attach
      the input user file (``--users-in``, else ``ALM_USERS_OUT``, else
      ``out/alm_users.json``) must exist and contain at least one user.

  run_pipeline
      with ``--skip-retrieve`` or ``--resume``: the same user-file check, since
      that file is what will be committed. Otherwise a dry run must have
      recorded an approved plan in ``out/pipeline_state.json`` -- this is what
      makes "dry run first" enforceable rather than merely documented.

  jts_unarchive_user
      allowed: it names one user explicitly on the command line.

Everything that is not a commit run of a write script is allowed untouched.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

STATE_PATH = "out/pipeline_state.json"

# script stem -> whether it consumes the shared user file
USER_FILE_SCRIPTS = {
    "jts_import_users": True,
    "elm_gpt": True,
    "ewm_comment_workitems": True,
    "jts_profile_attach": True,
}
ALWAYS_ALLOWED = {"jts_unarchive_user"}


def _command_text(payload: dict) -> str:
    """Best-effort extraction of the shell command from the hook payload."""
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(ti, dict):
        for key in ("command", "commandLine", "cmd", "script"):
            val = ti.get(key)
            if isinstance(val, str) and val:
                return val
    return json.dumps(payload.get("tool_input", ""))


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd, posix=False)
    except ValueError:
        return cmd.split()


def _is_commit(cmd: str) -> bool:
    return bool(re.search(r"--commit\b", cmd)) or bool(
        re.search(r"COMMIT\s*=\s*(1|true|yes)", cmd, re.I))


def _script(cmd: str) -> str:
    """The stem of the first *.py that looks like one of our entry points."""
    for token in _tokens(cmd):
        stem = os.path.splitext(os.path.basename(token.strip('"\'')))[0]
        if stem in USER_FILE_SCRIPTS or stem in ALWAYS_ALLOWED or stem == "run_pipeline":
            return stem
    return ""


def _users_path(cmd: str) -> str:
    """Honour an explicit --users-in / --users-out override."""
    tokens = _tokens(cmd)
    for flag in ("--users-in", "--users-out"):
        if flag in tokens:
            idx = tokens.index(flag)
            if idx + 1 < len(tokens):
                return tokens[idx + 1].strip('"\'')
    return os.getenv("ALM_USERS_OUT", "out/alm_users.json")


def _users_ok(path: str) -> tuple[bool, str]:
    if not os.path.isfile(path):
        return False, f"{path} does not exist"
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as err:
        return False, f"{path} could not be read as JSON ({err})"
    users = data.get("users", []) if isinstance(data, dict) else data
    if not isinstance(users, list) or not users:
        return False, f"{path} contains no users"
    return True, f"{len(users)} user(s) in {path}"


def _approved_plan_ok() -> tuple[bool, str]:
    if not os.path.isfile(STATE_PATH):
        return False, (f"no {STATE_PATH}: no dry run has been reviewed yet")
    try:
        with open(STATE_PATH, encoding="utf-8-sig") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as err:
        return False, f"{STATE_PATH} could not be read as JSON ({err})"
    plan = state.get("plan") or {}
    if not plan.get("hash"):
        return False, f"{STATE_PATH} holds no approved plan from a dry run"
    return True, (f"approved plan {plan['hash'][:12]} "
                  f"({plan.get('user_count', '?')} user(s), "
                  f"{plan.get('work_item_count', '?')} work item(s))")


def evaluate(cmd: str) -> tuple[bool, str]:
    """(allowed, reason). Pure, so the guard's rules are unit-testable."""
    if not _is_commit(cmd):
        return True, "not a commit run"
    script = _script(cmd)
    if not script:
        return True, "not a recognised write script"
    if script in ALWAYS_ALLOWED:
        return True, f"{script} names its target explicitly"

    if script == "run_pipeline":
        tokens = " " + cmd + " "
        if "--force-replan" in tokens:
            return True, "--force-replan explicitly overrides the approval gate"
        if "--skip-retrieve" in tokens or "--resume" in tokens:
            ok, detail = _users_ok(_users_path(cmd))
            return ok, detail
        ok, detail = _approved_plan_ok()
        if not ok:
            return False, (f"{detail}. Run 'python src/run_pipeline.py' (dry run), review "
                           "the plan, then re-run with --commit.")
        return True, detail

    ok, detail = _users_ok(_users_path(cmd))
    if not ok:
        return False, (f"{detail}. Run the alm-access-retrieval agent first to populate it.")
    return True, detail


def _deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0  # cannot parse - do not block

    allowed, reason = evaluate(_command_text(payload))
    if allowed:
        return 0
    _deny(f"Blocked --commit: {reason}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
