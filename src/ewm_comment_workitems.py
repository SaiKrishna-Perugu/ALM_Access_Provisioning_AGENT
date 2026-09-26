"""Add a JTS-import status comment to ALM Access Request work items in EWM.

For every work item referenced in out/alm_users.json, build a comment that lists
its users and their current JTS status, e.g.:

    User added to JTS :
    AB12345: FIRSTNAME LASTNAME: User added to JTS - (active)
    CD67890: FIRSTNAME LASTNAME: User added to JTS - (active)

Dry run (default) prints the comment that WOULD be posted for each work item and
changes nothing. Use --commit to post the comments to EWM.

Per-user status is read live from JTS (active / archived / not in JTS) using the
same helpers as the JTS import. Use --assume-active to skip the JTS lookup (no
JTS auth) and label every user "(active)".

Config (CID, EWM_SERVER, JTS_SERVER, ALM_USERS_OUT) comes from .env, as in the
sibling scripts. The password is prompted at runtime and never stored; the same
CID/password is used for both the JTS status lookup and the EWM comment post.

Usage:  python src/ewm_comment_workitems.py [--workitem ID] [--commit]
"""
from __future__ import annotations

import argparse
import getpass
import html
import json
import os
import sys

import requests
import urllib3

import alm_access_requests as aar
import alm_config
import alm_log
import audit
import idempotency
import jazz_client
import jts_import_users as jimp
import jts_unarchive_user as unarch

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# The old header asserted "User added to JTS" for every user on the work item,
# including the ones the import had reported as "already a JTS user (active)" and
# never touched. A comment is a permanent record on someone else's work item; it
# now states what this run actually did, per user.
COMMENT_HEADER = os.getenv("ALM_COMMENT_HEADER", "ALM access provisioning result :")

# import-step outcome -> the sentence that is true about that user.
ACTION_TEXT = {
    "created": "User added to JTS",
    "unarchived": "User reactivated in JTS (account was archived)",
    "already_active": "User already present in JTS - no change needed",
    "dry_run": "User would be imported into JTS",
    "unknown": "User access confirmed in JTS",
}


def action_for(record: dict | None) -> str:
    """Map an import audit record onto one of the ACTION_TEXT keys."""
    if not record:
        return "unknown"
    outcome = (record.get("outcome") or "").strip()
    return outcome if outcome in ACTION_TEXT else "unknown"


def status_from_audit(userids: list[str], rid: str = "") -> dict[str, dict]:
    """Per-user {action, state} derived from what the import step recorded.

    This is the fix for the system asserting an action it never performed: the
    text comes from this run's audit trail, not from a constant.
    """
    imported = audit.outcomes_for_step("import", rid)
    return {uid: {"action": action_for(imported.get(uid)), "state": "active"}
            for uid in userids}


def group_by_workitem(users: list[dict]):
    """Return (order, wi_users, wi_summary) grouping users under each work item.

    A user listed on several work items is included under each of them.
    """
    order: list[str] = []
    wi_users: dict[str, list[dict]] = {}
    wi_summary: dict[str, str] = {}
    for u in users:
        uid = (u.get("userid") or "").strip()
        if not uid:
            continue
        name = " ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x).strip() or uid
        for src in u.get("source_work_items", []) or []:
            wid = str(src.get("work_item_id") or "").strip()
            if not wid:
                continue
            if wid not in wi_users:
                wi_users[wid] = []
                wi_summary[wid] = (src.get("summary") or "").strip()
                order.append(wid)
            wi_users[wid].append({"userid": uid, "name": name})
    return order, wi_users, wi_summary


def jts_status_map(session, server: str, userids: list[str]) -> dict[str, str]:
    """Classify each user's live JTS state: active / archived / not in JTS."""
    status: dict[str, str] = {}
    for uid in userids:
        try:
            state, _info = jimp.resolve_user(session, server, uid)
        except requests.RequestException as err:
            status[uid] = f"lookup error - {err}"
            continue
        if state == "EXISTS":
            _etag, _rdf, archived = unarch.get_contributor(session, server, uid)
            status[uid] = "archived" if archived else "active"
        elif state == "READY":
            status[uid] = "not in JTS"
        elif state == "INVALID":
            status[uid] = "not in JTS (invalid)"
        else:  # MISSING
            status[uid] = "not in JTS (not in LDAP)"
    return status


def merge_status(audit_map: dict[str, dict], live_map: dict[str, str]) -> dict[str, dict]:
    """Combine the recorded action with the live JTS state, preferring live truth."""
    merged: dict[str, dict] = {}
    for uid, entry in audit_map.items():
        merged[uid] = dict(entry)
        if uid in live_map:
            merged[uid]["state"] = live_map[uid]
    for uid, state in live_map.items():
        merged.setdefault(uid, {"action": "unknown", "state": state})
    return merged


def _entry(status_map: dict, uid: str) -> dict:
    """Normalise a status map entry; a plain string is treated as a live state."""
    value = (status_map or {}).get(uid)
    if isinstance(value, dict):
        return {"action": value.get("action", "unknown"),
                "state": value.get("state", "unknown")}
    return {"action": "unknown", "state": value or "unknown"}


def build_comment(members: list[dict], status_map: dict, workitem_id: str = "") -> str:
    """Render the work-item comment, one accurate line per user.

    With ``workitem_id`` the comment ends with a marker that lets a later run
    recognise its own post and skip it instead of duplicating it.
    """
    lines = [COMMENT_HEADER]
    marked = []
    for m in members:
        uid = m["userid"]
        entry = _entry(status_map, uid)
        text = ACTION_TEXT.get(entry["action"], ACTION_TEXT["unknown"])
        lines.append(f"{uid}: {m['name']}: {text} - ({entry['state']})")
        marked.append({"userid": uid, "status": f"{entry['action']}/{entry['state']}"})
    if workitem_id:
        lines.append(idempotency.comment_marker(workitem_id, marked))
    return "\n".join(lines)


def all_reported(existing, members: list[dict], status_map: dict) -> bool:
    """True when every member's outcome already appears in a comment."""
    return bool(members) and all(
        idempotency.already_reported(
            existing, m["userid"],
            ACTION_TEXT.get(_entry(status_map, m["userid"])["action"],
                            ACTION_TEXT["unknown"]))
        for m in members)


def fetch_comments_url(session, ewm_server: str, uuid: str, wid: str) -> str:
    """Return the work item's OSLC discussion (comments) collection URL, by identifier."""
    query = f"{ewm_server}/oslc/contexts/{uuid}/workitems"
    params = {"oslc.where": f"dcterms:identifier={wid}", "oslc.properties": "dcterms:identifier"}
    r = session.get(
        query,
        headers={"Accept": "application/json", "OSLC-Core-Version": "2.0"},
        params=params,
        timeout=(15, 60),
    )
    if r.status_code != 200:
        raise RuntimeError(f"lookup failed: HTTP {r.status_code} - {r.text[:200]}")
    results = r.json().get("oslc:results", [])
    if not results:
        raise RuntimeError("work item not found")
    about = results[0].get("rdf:about")
    if not about:
        raise RuntimeError("work item resource URL missing")
    rr = session.get(
        about,
        headers={"Accept": "application/json", "OSLC-Core-Version": "2.0"},
        params={"oslc.properties": "oslc:discussedBy"},
        timeout=(15, 60),
    )
    if rr.status_code != 200:
        raise RuntimeError(f"resource read failed: HTTP {rr.status_code}")
    discussed = rr.json().get("oslc:discussedBy")
    url = discussed.get("rdf:resource") if isinstance(discussed, dict) else None
    if not url:
        raise RuntimeError("oslc:discussedBy (comments collection) not found on work item")
    return url


def comments_collection_url(ewm_server: str, wid: str) -> str:
    """Canonical work item comments (discussion) collection URL, numeric-id form."""
    return f"{ewm_server}/oslc/workitems/{wid}/rtc_cm:comments"


def comment_create_urls(collection_urls) -> list[str]:
    """Creation targets: the oslc:comment child factory of each comments collection.

    POSTing to the bare collection returns 405; the factory returns 201.
    """
    return list(dict.fromkeys(f"{u.rstrip('/')}/oslc:comment" for u in collection_urls if u))


def _descriptions(node, found: list[str]) -> list[str]:
    """Collect every dcterms:description string in an OSLC JSON payload.

    The comments collection is returned with different nesting depending on the
    endpoint that answered, so this walks the structure instead of assuming one
    shape.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("description") and isinstance(value, str):
                found.append(value)
            else:
                _descriptions(value, found)
    elif isinstance(node, list):
        for item in node:
            _descriptions(item, found)
    return found


def fetch_existing_comments(session, collection_urls) -> list[str] | None:
    """Text of every comment already on the work item, or None if unreadable.

    None is deliberately distinct from []: "no comments" permits a post, while
    "cannot tell" is recorded as an unverified idempotency check so an operator
    can see that a duplicate was possible.
    """
    if isinstance(collection_urls, str):
        collection_urls = [collection_urls]
    headers = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}
    for url in dict.fromkeys(u for u in collection_urls if u):
        try:
            r = session.get(url, headers=headers, timeout=jazz_client.DEFAULT_TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            return _descriptions(r.json(), [])
        except ValueError:
            continue
    return None


def post_comment(session, comment_urls, text: str):
    """Create a comment via the oslc:comment factory; author and timestamp come
    from the authenticated session."""
    if isinstance(comment_urls, str):
        comment_urls = [comment_urls]
    body = json.dumps({"dcterms:description": html.escape(text).replace("\n", "<br/>")}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "OSLC-Core-Version": "2.0",
        # Some EWM deployments reject POSTs without an explicit User-Agent.
        "User-Agent": "alm-access-retrieval/1.0",
    }
    attempts = []
    for url in dict.fromkeys(u for u in comment_urls if u):
        # Jazz CSRF prevention: POST is rejected unless this header carries the
        # session's JSESSIONID cookie value.
        jsessionid = session.cookies.get("JSESSIONID")
        if jsessionid:
            headers["X-Jazz-CSRF-Prevent"] = jsessionid
        try:
            # Retried on transport errors only, and only because the caller has
            # already checked the marker: a connection reset took out 4 of 17
            # comments mid-run on 2026-09-01 with no retry anywhere.
            r = jazz_client.post_with_retry(session, url, data=body, headers=headers)
        except requests.RequestException as err:
            attempts.append(f"{url}: {err}")
            continue
        if r.status_code in (200, 201):
            loc = r.headers.get("Location", "")
            return True, f"HTTP {r.status_code}{f' -> {loc}' if loc else ''}"
        attempts.append(f"{url}: HTTP {r.status_code} - {r.text[:160]}")
    return False, " | ".join(attempts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Add a JTS-import status comment to EWM work items.")
    ap.add_argument("--users-in", default=jimp.USERS_IN_DEFAULT, help="Input JSON (default out/alm_users.json).")
    ap.add_argument("--jts-server", default=jimp.JTS_SERVER, help="JTS base URL (for the status lookup).")
    ap.add_argument("--user", default=jimp.CID, help="CID username (default from .env CID).")
    ap.add_argument("--commit", action="store_true", help="Post the comments to EWM (else dry run).")
    ap.add_argument("--assume-active", action="store_true", help="Skip the JTS lookup; label every user active.")
    ap.add_argument("--workitem", action="append", default=None, metavar="ID",
                    help="Only process this work item ID (repeatable). Its comment uses only that item's users.")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N work items (0 = all).")
    args = ap.parse_args()

    users = jimp.load_users(args.users_in)
    if users is None:
        print(f"[STOP] Input file not found: {args.users_in}")
        print("       Run the alm-access-retrieval agent first to generate it.")
        return 1
    if not users:
        print(f"[STOP] No users in {args.users_in}. Run the alm-access-retrieval agent first.")
        return 1

    order, wi_users, wi_summary = group_by_workitem(users)
    if args.workitem:
        wanted = {w.strip() for w in args.workitem}
        missing = wanted - set(order)
        if missing:
            print(f"[STOP] Work item(s) not found in {args.users_in}: {', '.join(sorted(missing))}")
            return 1
        order = [wid for wid in order if wid in wanted]
    if args.limit > 0:
        order = order[: args.limit]

    alm_config.print_banner("comment (EWM work items)", commit=args.commit)
    print(f"Input      : {args.users_in}")
    print(f"EWM server : {aar.SERVER}")
    print(f"Work items : {len(order)}")
    print(f"Mode       : {'COMMIT' if args.commit else 'DRY RUN'}")
    print()

    # One password serves both the JTS status lookup and the EWM update (same CID).
    password = None
    if not args.assume_active or args.commit:
        if not args.user:
            print("[STOP] No username. Set CID in .env or pass --user.")
            return 1
        # EWM_PASSWORD lets the pipeline orchestrator prompt once for all steps.
        password = os.getenv("EWM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")

    # Resolve what to say about each user. The action comes from what the import
    # step recorded in this run's audit; --assume-active only skips the extra live
    # JTS state lookup, it no longer invents the action.
    unique_ids = sorted({m["userid"] for wid in order for m in wi_users[wid]})
    status_map = status_from_audit(unique_ids)
    if not args.assume_active:
        jts_server = args.jts_server.rstrip("/")
        jsession = jazz_client.make_session()
        if not jimp.login(jsession, args.user, password, jts_server):
            return 2
        status_map = merge_status(status_map, jts_status_map(jsession, jts_server, unique_ids))

    # Build and show the planned comments.
    comments = {wid: build_comment(wi_users[wid], status_map, wid) for wid in order}
    for wid in order:
        print("=" * 70)
        print(f"Work Item {wid}" + (f" - {wi_summary[wid]}" if wi_summary[wid] else ""))
        print("-" * 70)
        print(comments[wid])
    print("=" * 70)

    if not args.commit:
        for wid in order:
            for m in wi_users[wid]:
                audit.record("comment", m["userid"], "skipped", outcome="dry_run",
                             message=f"would comment on {wid}", workitem=wid)
        audit.flush("comment")
        print("\nDRY RUN - no work item was modified. Re-run with --commit to post these comments.")
        return 0

    if not alm_config.confirm_prod_write(
            f"Commenting on {len(order)} work item(s)"):
        audit.record_not_attempted(
            "comment", sorted({m["userid"] for wid in order for m in wi_users[wid]}),
            "production write not confirmed")
        audit.flush("comment")
        return 1

    # Commit: authenticate to EWM and post each comment.
    try:
        esession = aar.login(args.user, password)
        uuid = aar.project_uuid(esession)
    except Exception as err:  # noqa: BLE001
        print(f"\n[ERROR] {err}")
        if isinstance(err, requests.exceptions.ConnectionError | requests.exceptions.Timeout):
            print("Ensure you are on the Chrysler intranet / VPN.")
        return 1

    posted = failed = duplicate = 0
    for wid in order:
        skipped_as_duplicate = False
        try:
            urls = []
            try:
                urls.append(fetch_comments_url(esession, aar.SERVER, uuid, wid))
            except Exception:  # noqa: BLE001 - fall back to the numeric-id form
                pass
            urls.append(comments_collection_url(aar.SERVER, wid))

            # Idempotency: recognise this run's own marker and do not post twice.
            marker = idempotency.comment_marker(
                wid, [{"userid": m["userid"],
                       "status": "{}/{}".format(_entry(status_map, m["userid"])["action"],
                                                _entry(status_map, m["userid"])["state"])}
                      for m in wi_users[wid]])
            existing = fetch_existing_comments(esession, urls)
            if existing is not None and idempotency.already_commented(existing, marker):
                skipped_as_duplicate = True
                ok, msg = True, f"identical comment already present {marker}"
            elif existing is not None and all_reported(existing, wi_users[wid], status_map):
                # Already said by an earlier run or by the agents (whose
                # comments carry no marker): do not say it twice.
                skipped_as_duplicate = True
                ok, msg = True, "every user's outcome is already reported on this work item"
            else:
                if existing is None:
                    alm_log.warn(f"[warn] {wid}: could not read existing comments; "
                                 "posting without a duplicate check.",
                                 "idempotency_unverified", workitem=wid)
                ok, msg = post_comment(esession, comment_create_urls(urls), comments[wid])
        except Exception as err:  # noqa: BLE001
            ok, msg = False, str(err)
        if ok and skipped_as_duplicate:
            print(f"[SKIP] {wid}: {msg} - not posting again.")
            duplicate += 1
        elif ok:
            print(f"[OK]   {wid}: comment posted ({msg}).")
            posted += 1
        else:
            print(f"[FAIL] {wid}: {msg}")
            failed += 1
        for m in wi_users[wid]:
            if skipped_as_duplicate:
                audit.record("comment", m["userid"], audit.SKIPPED,
                             outcome="already_commented", message=msg, workitem=wid)
            else:
                audit.record("comment", m["userid"], audit.OK if ok else audit.FAILED,
                             outcome="posted" if ok else "post_failed",
                             message=msg, workitem=wid)

    audit.flush("comment")
    print()
    print(f"Summary: {posted} posted, {duplicate} already present, {failed} failed "
          f"(of {len(order)}).")
    alm_log.event("comment_summary", posted=posted, duplicate=duplicate, failed=failed,
                  work_items=len(order))
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
