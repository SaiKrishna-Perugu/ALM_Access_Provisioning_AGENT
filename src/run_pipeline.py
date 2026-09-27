"""Orchestrate the ALM provisioning pipeline end to end.

Steps (each also runs standalone as its own script):
  1. retrieve  - alm_access_requests.py  -> out/alm_users.json
  2. gpt       - elm_gpt.py (always runs unless --skip-gpt; needs the debug Chrome
                 from start-gpt.ps1)
  3. import    - jts_import_users.py (creates/unarchives JTS contributors)
  4. verify    - jts_permission.py helpers: poll until every user has the JazzUsers
                 repository permission (default: every 5 min, up to 30 min)
  5. comment   - ewm_comment_workitems.py, VERIFIED users only
  6. attach    - jts_profile_attach.py, VERIFIED users only

Users that never verify are excluded from steps 5-6 and listed in a final
report - nothing is posted for them.

Dry run (default) runs every step in its dry-run form and does a single
permission check instead of the 30-minute poll. --commit makes each step write.

**The approval gate is binding.** A dry run fingerprints the plan it showed you
into out/pipeline_state.json. A --commit run re-fingerprints what it retrieved
and refuses to write if the queue changed in between, so the plan you approved
is the plan that executes. Use --skip-retrieve to commit exactly the reviewed
set, or re-run the dry run to review the new queue.

The password is prompted ONCE and handed to the child steps via the
EWM_PASSWORD environment variable of this process (never stored on disk).
State is checkpointed to out/pipeline_state.json; --resume skips completed steps
(but never skips verification - that is the safety gate, and it is cheap).
Every step appends per-user outcomes to out/audit/run-<id>.json, summarised in a
success/failure table at the end of the run.

Exit codes: 0 clean, 1 a step failed or input was invalid, 2 authentication,
3 the run completed but the audit contains failures or permission timeouts.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time

import alm_config
import alm_log
import audit
import jazz_client
import jts_import_users as jimp
import jts_permission as perm
import plan_lock

SRC = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = "out/pipeline_state.json"
VERIFIED_PATH = "out/alm_users_verified.json"
STEPS = ["retrieve", "gpt", "import", "verify", "comment", "attach"]


def load_state(resume: bool) -> dict:
    """Read the checkpoint file, refusing to resume across schema or environment.

    Resuming a PROD run into a TEST configuration (or the reverse) would post
    one environment's results onto the other's work items. The .env server line
    was toggled four times in a single day during development, so this is a
    realistic accident, not a theoretical one.
    """
    fresh = {"schema": alm_config.STATE_SCHEMA, "env": alm_config.alm_env(),
             "steps": {}, "verified": {}, "plan": None}
    if not resume or not os.path.exists(STATE_PATH):
        return fresh
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as err:
        print(f"[STOP] {STATE_PATH} could not be read ({err}). Re-run without --resume.")
        raise SystemExit(1) from err

    schema = state.get("schema")
    if schema != alm_config.STATE_SCHEMA:
        print(f"[STOP] {STATE_PATH} was written by schema {schema!r}, this build expects "
              f"{alm_config.STATE_SCHEMA}. Delete it and start a fresh run.")
        raise SystemExit(1)

    saved_env, current_env = state.get("env"), alm_config.alm_env()
    if saved_env != current_env:
        print(f"[STOP] {STATE_PATH} belongs to a {saved_env} run but this process is "
              f"configured for {current_env}. Refusing to resume across environments - "
              "check EWM_SERVER / JTS_SERVER in .env.")
        raise SystemExit(1)

    state.setdefault("steps", {})
    state.setdefault("verified", {})
    state.setdefault("plan", None)
    return state


def save_state(state: dict) -> None:
    state["schema"] = alm_config.STATE_SCHEMA
    state["env"] = alm_config.alm_env()
    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    os.makedirs("out", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def run_step(name: str, cmd: list[str], state: dict, ok_codes: tuple[int, ...] = (0,)) -> bool:
    """Run one step as a subprocess. ``ok_codes`` are the codes that let the run continue."""
    print(f"\n{'=' * 70}\nSTEP {name}: {' '.join(os.path.basename(c) for c in cmd[:2])} "
          f"{' '.join(cmd[2:])}\n{'-' * 70}", flush=True)
    rc = subprocess.call(cmd)  # noqa: S603 - pipeline orchestrator executes fixed internal step commands
    ok = rc in ok_codes
    state["steps"][name] = {"done": ok, "rc": rc, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_state(state)
    alm_log.event("step_finished", step=name, rc=rc, ok=ok)
    if not ok:
        print(f"[STOP] step '{name}' failed (exit {rc}).")
    elif rc != 0:
        # e.g. import exit 3: some users failed, the rest are still worth finishing.
        print(f"[warn] step '{name}' reported partial failure (exit {rc}); "
              "continuing with the users that succeeded.")
    return ok


def write_verified(users_in: str, verified: dict[str, bool], plan: dict | None) -> int:
    """Write the verified-user subset (structure preserved) for the comment/attach steps.

    Stamped with the run id and plan fingerprint so a stale file from an earlier
    run cannot be silently consumed by a later one.
    """
    users = jimp.load_users(users_in) or []
    subset = [u for u in users if verified.get((u.get("userid") or "").strip())]
    os.makedirs("out", exist_ok=True)
    with open(VERIFIED_PATH, "w", encoding="utf-8") as fh:
        json.dump({"schema": alm_config.STATE_SCHEMA,
                   "run_id": audit.run_id(),
                   "env": alm_config.alm_env(),
                   "plan_hash": (plan or {}).get("hash", ""),
                   "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "count": len(subset),
                   "users": subset}, fh, indent=2)
    return len(subset)


def finalize(verified: dict[str, bool] | None = None, meta: dict | None = None,
             note: str = "", dry_run: bool = False, plan: dict | None = None) -> int:
    """Merge every step's audit file, print the report, and derive the exit code."""
    extra: dict = {}
    if meta:
        extra["permission_poll"] = meta
    if plan:
        extra["plan"] = plan
    extra["mode"] = "dry_run" if dry_run else "commit"
    path, records = audit.aggregate(extra=extra or None)
    print(f"\n{'=' * 70}\nAUDIT REPORT\n{'-' * 70}")
    print(audit.summary_table(records, dry_run=dry_run))
    print(f"\nAudit log  : {path}")
    print(f"Run log    : out/logs/{audit.run_id()}.jsonl")

    unverified = [u for u, ok in (verified or {}).items() if not ok]
    if unverified and meta:
        polled = meta["wait_min"] > 0
        print(f"\n{'TIMEOUT' if polled else 'PENDING'}    : {len(unverified)} user(s) without the "
              f"{meta['role']} permission")
        print(f"  Attempts performed    : {meta['attempts']}"
              + (f"/{meta.get('max_attempts', meta['attempts'])} "
                 f"(every {meta['interval_min']}m, cap {meta['wait_min']}m)" if polled
                 else " (single check - dry run does not poll)"))
        print(f"  Last permission check : {meta['last_check']}")
        print(f"  User IDs              : {', '.join(unverified)}")
        print("  Nothing was posted to their work items.")
        print(f"  Re-check: python src/jts_permission.py {' '.join(unverified)}")
    if note:
        print(f"\n{note}")
    return 3 if audit.failures(records) else 0


def resolve_plan(users_in: str, state: dict, commit: bool, force_replan: bool) -> dict | None:
    """Fingerprint the retrieved plan and enforce the approval gate.

    Returns the current plan summary, or None when the run must abort.
    """
    users = jimp.load_users(users_in) or []
    current = plan_lock.plan_summary(users)
    previous = state.get("plan")

    if not commit:
        state["plan"] = current
        save_state(state)
        print(f"\nPlan       : {current['user_count']} user(s) across "
              f"{current['work_item_count']} work item(s)  [{current['hash'][:12]}]")
        print("             Approve this plan by re-running with --commit.")
        return current

    if force_replan:
        print(f"[warn] --force-replan: committing {current['user_count']} user(s) without "
              "matching an approved dry run.")
        state["plan"] = current
        save_state(state)
        return current

    ok, message = plan_lock.check(previous, current)
    if not ok:
        print(f"\n{'=' * 70}\n[STOP] APPROVAL GATE\n{'-' * 70}")
        print(message)
        alm_log.event("plan_gate_blocked", level="error",
                      approved=(previous or {}).get("hash", ""), retrieved=current["hash"])
        return None
    print(f"\nPlan       : {message} [{current['hash'][:12]}]")
    return current


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the full ALM provisioning pipeline.")
    ap.add_argument("--users-in", default=jimp.USERS_IN_DEFAULT,
                    help="User IDs JSON (default out/alm_users.json).")
    ap.add_argument("--user", default=jimp.CID, help="CID username (default from .env CID).")
    ap.add_argument("--skip-retrieve", action="store_true",
                    help="Reuse the existing --users-in file instead of querying EWM. "
                         "Commits exactly the set that was reviewed.")
    ap.add_argument("--skip-gpt", action="store_true",
                    help="Skip the GPT AD-group step (it otherwise always runs and "
                         "needs the debug Chrome from scripts/start-gpt.ps1).")
    ap.add_argument("--workitem", action="append", default=None, metavar="ID",
                    help="Only process this work item in the comment/attach steps (repeatable).")
    ap.add_argument("--wait", type=int, default=30, metavar="MIN",
                    help="Max minutes to wait for permission propagation (default 30).")
    ap.add_argument("--interval", type=int, default=5, metavar="MIN",
                    help="Minutes between permission checks (default 5).")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="Check N users in parallel during verification (default 1).")
    ap.add_argument("--resume", action="store_true",
                    help="Continue from the last completed step in out/pipeline_state.json.")
    ap.add_argument("--force-replan", action="store_true",
                    help="Commit even though the retrieved queue no longer matches the "
                         "approved dry run. Every added user is written to production.")
    ap.add_argument("--commit", action="store_true",
                    help="Perform the writes in every step (else all steps dry-run).")
    args = ap.parse_args()

    if not args.user:
        print("[STOP] No username. Set CID in .env or pass --user.")
        return 1

    py = sys.executable
    commit = ["--commit"] if args.commit else []
    wi = [x for w in (args.workitem or []) for x in ("--workitem", w)]
    state = load_state(args.resume)
    done = {n for n, s in state["steps"].items() if s.get("done")}

    alm_config.print_banner("pipeline", commit=args.commit)
    print(f"Pipeline   : {' -> '.join(s for s in STEPS if s != 'gpt' or not args.skip_gpt)}")
    print(f"Mode       : {'COMMIT' if args.commit else 'DRY RUN'}"
          + (f"   (resuming past: {', '.join(sorted(done))})" if done else ""))
    # Children inherit the run id so every step appends to the same audit trail.
    print(f"Run id     : {audit.run_id()}")
    alm_log.event("pipeline_start", commit=args.commit, env=alm_config.alm_env(),
                  skip_gpt=args.skip_gpt, resume=args.resume)

    # One production confirmation for the whole run; children inherit it through
    # the environment so each step does not re-prompt.
    if args.commit and not alm_config.confirm_prod_write("This pipeline run"):
        return 1

    # One prompt for every step (children read EWM_PASSWORD; same CID everywhere).
    if not os.getenv("EWM_PASSWORD"):
        os.environ["EWM_PASSWORD"] = getpass.getpass(f"Password for {args.user}: ")

    if "retrieve" not in done:
        if args.skip_retrieve:
            if not os.path.exists(args.users_in):
                print(f"[STOP] --skip-retrieve set but {args.users_in} does not exist.")
                return 1
            state["steps"]["retrieve"] = {"done": True, "skipped": True}
            save_state(state)
        elif not run_step("retrieve", [py, os.path.join(SRC, "alm_access_requests.py"),
                                       "--users-out", args.users_in], state):
            finalize(note="[STOP] retrieve step failed.", dry_run=not args.commit)
            return 1

    # The approval gate. --skip-retrieve commits the reviewed file unchanged, so
    # it is trusted by definition; anything else must match the approved plan.
    plan = state.get("plan")
    if not (args.commit and args.skip_retrieve):
        plan = resolve_plan(args.users_in, state, args.commit, args.force_replan)
        if plan is None:
            finalize(note="[STOP] the approved plan and the live queue disagree.",
                     dry_run=not args.commit)
            return 1

    for u in jimp.load_users(args.users_in) or []:
        if u.get("userid"):
            audit.record("retrieve", u["userid"], audit.OK, outcome="retrieved",
                         message=u.get("email", ""))
    audit.flush("retrieve")

    if not args.skip_gpt and "gpt" not in done and \
            not run_step("gpt", [py, os.path.join(SRC, "elm_gpt.py"),
                                 "--users-in", args.users_in] + commit, state):
            finalize(note="[STOP] GPT step failed.", dry_run=not args.commit, plan=plan)
            return 1

    # Exit 3 means "some users failed"; the run continues for the rest, and the
    # audit report names every failure. Exit 4 (all failed) stops it.
    if "import" not in done and \
            not run_step("import", [py, os.path.join(SRC, "jts_import_users.py"),
                                    "--users-in", args.users_in] + commit, state,
                         ok_codes=(0, 3)):
            finalize(note="[STOP] import step failed.", dry_run=not args.commit, plan=plan)
            return 1

    # verify: in-process (single login; poll only in commit mode). Deliberately
    # re-run even with --resume: it is the gate that decides who gets written
    # about, it costs one request per user, and a cached "verified" from an
    # earlier run is exactly the kind of stale fact this pipeline must not trust.
    users = jimp.load_users(args.users_in) or []
    uids = sorted({(u.get("userid") or "").strip() for u in users if u.get("userid")})
    if not uids:
        print(f"[STOP] No users in {args.users_in}.")
        return 1
    print(f"\n{'=' * 70}\nSTEP verify: JazzUsers permission for {len(uids)} user(s)\n{'-' * 70}")
    session = jazz_client.make_session()
    if not jimp.login(session, args.user, os.environ["EWM_PASSWORD"], jimp.JTS_SERVER):
        return 2
    wait = args.wait if args.commit else 0  # dry run: single check, no 30-min poll
    verified, meta = perm.poll_roles(session, jimp.JTS_SERVER, uids, wait_min=wait,
                                     interval_min=args.interval, workers=args.workers)
    for uid, ok in verified.items():
        if ok:
            audit.record("verify", uid, audit.OK, outcome="permission_granted",
                         message=f"{meta['role']} after {meta['attempts']} check(s)",
                         attempts=meta["attempts"], last_check=meta["last_check"])
        else:
            # A dry run does a single check, so only a real poll can time out.
            polled = wait > 0
            audit.record("verify", uid, audit.TIMEOUT if polled else audit.SKIPPED,
                         outcome="permission_timeout" if polled else "permission_pending",
                         message=f"no {meta['role']} after {meta['attempts']} check(s)",
                         attempts=meta["attempts"], last_check=meta["last_check"])
    audit.flush("verify")
    state["verified"] = verified
    state["steps"]["verify"] = {"done": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save_state(state)

    n = write_verified(args.users_in, verified, plan)
    unverified = [u for u, ok in verified.items() if not ok]
    if n == 0:
        print("\n[STOP] No user verified - nothing to comment/attach.")
        return finalize(verified, meta, dry_run=not args.commit, plan=plan)

    if "comment" not in done and \
            not run_step("comment", [py, os.path.join(SRC, "ewm_comment_workitems.py"),
                                     "--users-in", VERIFIED_PATH, "--assume-active"]
                         + wi + commit, state):
            finalize(verified, meta, note="[STOP] comment step failed.",
                     dry_run=not args.commit, plan=plan)
            return 1

    if "attach" not in done and \
            not run_step("attach", [py, os.path.join(SRC, "jts_profile_attach.py"),
                                    "--users-in", VERIFIED_PATH] + wi + commit, state):
            finalize(verified, meta, note="[STOP] attach step failed.",
                     dry_run=not args.commit, plan=plan)
            return 1

    print(f"\n{'=' * 70}\nPIPELINE {'COMPLETE' if not unverified else 'COMPLETE WITH SKIPS'}")
    print(f"Verified   : {n}/{len(uids)} user(s) -> commented + attached")
    return finalize(verified, meta, dry_run=not args.commit, plan=plan)


if __name__ == "__main__":
    sys.exit(main())
