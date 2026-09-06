"""Check (and poll) JTS repository permissions for user IDs.

Reads each contributor's details via the same internal service the JTS admin UI
uses (IAdminRestService/contributorByUserId), which returns the repository
"roles" list - e.g. ["JazzUsers"] - plus the archived flag.

A user "has the permission" when the role is present AND the account is not
archived. --wait polls until every user verifies or the cap elapses (permission
propagation can take up to ~30 minutes after import).

Exit codes: 0 all verified, 3 some users unverified, 1/2 setup or auth errors.

Usage:
  python src/jts_permission.py SF58083 T0195G3            # single check
  python src/jts_permission.py SF58083 --wait 30 --interval 5   # poll
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

import urllib3

import alm_config
import alm_log
import jazz_client
import jts_import_users as jimp

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DETAILS_PATH = "/service/com.ibm.team.repository.service.internal.IAdminRestService/contributorByUserId"
DEFAULT_ROLE = "JazzUsers"


def contributor_details(session, server: str, uid: str) -> dict | None:
    """Return the ContributorDetailsDTO dict for a user ID, or None if not found."""
    r = session.get(f"{server}{DETAILS_PATH}", params={"userId": uid},
                    headers=jimp.XHR_HEADERS, timeout=jazz_client.DEFAULT_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        value = r.json()["soapenv:Body"]["response"]["returnValue"]["value"]
    except (KeyError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("userId") == uid else None


def has_role(details: dict | None, role: str = DEFAULT_ROLE) -> bool:
    """True when the role is assigned and the contributor is not archived."""
    if not details:
        return False
    return role in (details.get("roles") or []) and not details.get("archived", False)


def max_attempts(wait_min: int, interval_min: int) -> int:
    """How many checks a poll performs: one at t=0 then one per interval up to the cap.

    ``--wait 30 --interval 5`` is 7 checks (t=0,5,10,15,20,25,30), not 6. The
    count was previously undocumented and the report disagreed with the code;
    both now derive from this function.
    """
    if wait_min <= 0 or interval_min <= 0:
        return 1
    return wait_min // interval_min + 1


def _check_all(session, server: str, uids: list[str], role: str, workers: int) -> dict[str, bool]:
    """One verification pass over the pending users."""
    if workers <= 1 or len(uids) <= 1:
        return {uid: has_role(contributor_details(session, server, uid), role) for uid in uids}
    # Opt-in concurrency: 17 users x 7 checks is 119 serial round trips. Kept off
    # by default because the Jazz server's tolerance for parallel admin-service
    # calls has not been measured on this deployment.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(workers, len(uids))) as pool:
        results = pool.map(lambda uid: (uid, has_role(contributor_details(session, server, uid), role)),
                           uids)
        return dict(results)


def poll_roles(session, server: str, uids: list[str], role: str = DEFAULT_ROLE,
               wait_min: int = 0, interval_min: int = 5,
               workers: int = 1) -> tuple[dict[str, bool], dict]:
    """Check every user, re-checking pending ones until verified or wait_min elapses.

    Returns (verified, meta); meta carries the attempt count, the planned maximum
    and the timestamp of every check so a caller can report exactly how long it
    waited and whether it waited as long as it promised.
    """
    verified = dict.fromkeys(uids, False)
    deadline = time.time() + wait_min * 60
    checks: list[dict] = []
    attempt = 0
    while True:
        attempt += 1
        verified.update(_check_all(session, server,
                                   [u for u, ok in verified.items() if not ok], role, workers))
        pending = [u for u, ok in verified.items() if not ok]
        done = len(uids) - len(pending)
        checks.append({
            "attempt": attempt,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "verified": [u for u, ok in verified.items() if ok],
            "pending": list(pending),
        })
        print(f"[check {attempt}] {done}/{len(uids)} verified"
              + (f", pending: {', '.join(pending)}" if pending else ""))
        if not pending or time.time() >= deadline:
            break
        sleep_s = min(interval_min * 60, max(1, int(deadline - time.time())))
        print(f"           waiting {sleep_s // 60}m{sleep_s % 60:02d}s before re-check...")
        time.sleep(sleep_s)
    meta = {
        "attempts": attempt,
        "max_attempts": max_attempts(wait_min, interval_min),
        "checks": checks,
        "last_check": checks[-1]["timestamp"] if checks else "",
        "role": role,
        "wait_min": wait_min,
        "interval_min": interval_min,
        "workers": workers,
    }
    alm_log.event("permission_poll", role=role, attempts=attempt,
                  verified=[u for u, ok in verified.items() if ok],
                  pending=[u for u, ok in verified.items() if not ok])
    return verified, meta


def main() -> int:
    ap = argparse.ArgumentParser(description="Check/poll JTS repository permissions for users.")
    ap.add_argument("userids", nargs="+", help="User IDs to check.")
    ap.add_argument("--server", default=jimp.JTS_SERVER, help="JTS base URL.")
    ap.add_argument("--user", default=jimp.CID, help="CID username (default from .env CID).")
    ap.add_argument("--role", default=DEFAULT_ROLE, help=f"Required role (default {DEFAULT_ROLE}).")
    ap.add_argument("--wait", type=int, default=0, metavar="MIN",
                    help="Poll up to this many minutes (default 0 = single check).")
    ap.add_argument("--interval", type=int, default=5, metavar="MIN",
                    help="Minutes between checks when polling (default 5).")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="Check N users in parallel (default 1 = sequential).")
    args = ap.parse_args()

    if not args.user:
        print("[STOP] No username. Set CID in .env or pass --user.")
        return 1
    server = args.server.rstrip("/")
    alm_config.print_banner("verify (JTS permission)", commit=False)
    print(f"JTS server : {server}")
    print(f"Role       : {args.role}")
    print(f"Users      : {', '.join(args.userids)}")

    # EWM_PASSWORD lets the pipeline orchestrator prompt once for all steps.
    password = os.getenv("EWM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    session = jazz_client.make_session()
    if not jimp.login(session, args.user, password, server):
        return 2

    verified, meta = poll_roles(session, server, args.userids, args.role, args.wait,
                                args.interval, args.workers)
    print()
    for uid, ok in verified.items():
        print(f"  {uid}: {'VERIFIED - has ' + args.role if ok else 'NOT VERIFIED'}")
    print(f"\nChecks: {meta['attempts']}   Last check: {meta['last_check']}")
    return 0 if all(verified.values()) else 3


if __name__ == "__main__":
    sys.exit(main())
