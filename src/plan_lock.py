"""Bind the plan a human approved to the plan the pipeline executes.

The approval gate used to be advisory. The operator reviewed a dry run, said
yes, and the commit run then re-queried the *live* EWM queue from scratch --
so anything raised in between was swept into a commit nobody reviewed. On
2026-08-27 that gap ran from 4 work items / 7 users at 14:52 to 13 work items /
17 users by 00:45 the same day.

A plan is now fingerprinted at dry-run time and stored in the state file. A
commit run re-fingerprints what it retrieved and refuses to write when the two
disagree, naming exactly which users and work items appeared or vanished.

The fingerprint deliberately covers only what an approver actually approves --
which user IDs get provisioned, and which work items get written to. A user's
display name being corrected in LDAP between the two runs is not a plan change
and must not force a re-approval; a seventeenth user appearing is.
"""
from __future__ import annotations

import hashlib
import json


def _canonical(users: list[dict]) -> list[list]:
    """[[userid, [work item ids...]], ...] sorted, for a stable fingerprint."""
    rows: dict[str, set[str]] = {}
    for user in users or []:
        uid = (user.get("userid") or "").strip()
        if not uid:
            continue
        items = rows.setdefault(uid, set())
        for src in user.get("source_work_items") or []:
            wid = str(src.get("work_item_id") or "").strip()
            if wid:
                items.add(wid)
    return [[uid, sorted(rows[uid])] for uid in sorted(rows)]


def plan_hash(users: list[dict]) -> str:
    """Stable sha256 over the (user -> work items) mapping."""
    blob = json.dumps(_canonical(users), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def plan_summary(users: list[dict]) -> dict:
    """The fingerprint plus the numbers an operator sees in the approval prompt."""
    canonical = _canonical(users)
    work_items = sorted({wid for _uid, wids in canonical for wid in wids})
    return {
        "hash": plan_hash(users),
        "users": [uid for uid, _ in canonical],
        "user_count": len(canonical),
        "work_items": work_items,
        "work_item_count": len(work_items),
    }


def diff(previous: dict, current: dict) -> list[str]:
    """Human-readable differences between two plan summaries."""
    old_users, new_users = set(previous.get("users") or []), set(current.get("users") or [])
    old_wis = set(previous.get("work_items") or [])
    new_wis = set(current.get("work_items") or [])
    lines = []
    for label, added, removed in (
        ("user", sorted(new_users - old_users), sorted(old_users - new_users)),
        ("work item", sorted(new_wis - old_wis), sorted(old_wis - new_wis)),
    ):
        if added:
            lines.append(f"  + {len(added)} {label}(s) appeared since approval: "
                         f"{', '.join(added)}")
        if removed:
            lines.append(f"  - {len(removed)} {label}(s) vanished since approval: "
                         f"{', '.join(removed)}")
    if not lines:
        lines.append("  (the user/work-item mapping is unchanged but the fingerprint "
                     "differs - the stored plan may predate a format change)")
    return lines


def check(previous: dict | None, current: dict) -> tuple[bool, str]:
    """Compare a stored approved plan against what a commit run just retrieved.

    Returns (ok, message). ``ok=False`` must abort the run: proceeding would
    execute work the operator never saw.
    """
    if not previous or not previous.get("hash"):
        return False, (
            "No approved plan on file. Run the dry run first "
            "(python src/run_pipeline.py), review it, then re-run with --commit.")
    if previous["hash"] == current["hash"]:
        return True, (f"Plan matches the approved dry run "
                      f"({current['user_count']} user(s), "
                      f"{current['work_item_count']} work item(s)).")
    detail = "\n".join(diff(previous, current))
    return False, (
        "The queue changed after the plan was approved - refusing to commit work "
        "that was never reviewed.\n"
        f"  approved : {previous.get('user_count', '?')} user(s), "
        f"{previous.get('work_item_count', '?')} work item(s)  [{previous['hash'][:12]}]\n"
        f"  retrieved: {current['user_count']} user(s), "
        f"{current['work_item_count']} work item(s)  [{current['hash'][:12]}]\n"
        f"{detail}\n"
        "  Re-run the dry run to review the new queue, or commit the reviewed set "
        "unchanged with --skip-retrieve.")
