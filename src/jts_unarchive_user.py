"""Unarchive (reactivate) a JTS contributor via the Jazz Foundation user REST API.

Read-only by default: it fetches the contributor RDF, shows the current archived
state, and prints what WOULD change. Use --commit to actually flip
jfs:archived=false with a conditional (If-Match) PUT, then re-reads to confirm.

Auth reuses the same Jazz form-auth flow as jts_import_users.py. The password is
prompted at runtime (never stored). This is a WRITE to the shared JTS registry, so
--commit is required and the change is reported before and after.
"""
import argparse
import getpass
import os
import re
import sys

import urllib3

import alm_config
import jazz_client
import jts_import_users as j

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RDF = "application/rdf+xml"
_ARCHIVED_RE = re.compile(
    r"(<jfs:archived\b[^>]*>)\s*(true|false)\s*(</jfs:archived>)", re.I | re.S
)


def get_contributor(session, server, uid):
    """Return (etag, rdf_text, archived_bool) for a JTS contributor, or (None, None, None)."""
    r = session.get(f"{server}/users/{uid}", headers={"Accept": RDF}, allow_redirects=True,
                    timeout=jazz_client.DEFAULT_TIMEOUT)
    ct = r.headers.get("Content-Type", "").lower()
    if r.status_code != 200 or "html" in ct or "rdf" not in ct:
        return None, None, None
    m = _ARCHIVED_RE.search(r.text)
    archived = (m.group(2).lower() == "true") if m else None
    return r.headers.get("ETag"), r.text, archived


def set_archived_false(rdf_text):
    """Return rdf with jfs:archived set to false (or None if the element is absent)."""
    if not _ARCHIVED_RE.search(rdf_text):
        return None
    return _ARCHIVED_RE.sub(lambda m: m.group(1) + "false" + m.group(3), rdf_text, count=1)


def unarchive(session, server, uid, etag, new_rdf):
    """Conditional PUT of the modified contributor RDF. Returns (ok, message)."""
    headers = {"Content-Type": RDF, "Accept": RDF, "X-Requested-With": "XMLHttpRequest"}
    if etag:
        # JFS 7.0.2 SR1 wants the concurrency token in a header named "ETag" (the
        # quoted value from the GET); it rejects the standard "If-Match" with
        # CRJZS5488E. Send both so it works across versions.
        headers["ETag"] = etag
        headers["If-Match"] = etag
    r = session.put(f"{server}/users/{uid}", data=new_rdf.encode("utf-8"), headers=headers,
                    timeout=jazz_client.DEFAULT_TIMEOUT)
    if r.status_code in (200, 204):
        return True, f"HTTP {r.status_code}"
    return False, f"HTTP {r.status_code} - {r.text[:200]}"


def main():
    ap = argparse.ArgumentParser(description="Unarchive (reactivate) a JTS contributor.")
    ap.add_argument("userid", help="The JTS user ID to unarchive (e.g. AB12345).")
    ap.add_argument("--server", default=j.JTS_SERVER, help="JTS base URL.")
    ap.add_argument("--user", default=j.CID, help="JTS username (CID).")
    ap.add_argument("--commit", action="store_true", help="Actually perform the unarchive PUT.")
    args = ap.parse_args()

    uid = args.userid.strip()
    server = args.server.rstrip("/")
    alm_config.print_banner("unarchive (JTS contributor)", commit=args.commit)
    print(f"JTS server : {server}")
    print(f"User       : {uid}")

    session = jazz_client.make_session()
    # EWM_PASSWORD lets the pipeline orchestrator prompt once for all steps.
    pwd = os.getenv("EWM_PASSWORD") or getpass.getpass(f"JTS password for {args.user}: ")
    if not j.login(session, args.user, pwd, server):
        sys.exit(1)

    etag, rdf_text, archived = get_contributor(session, server, uid)
    if rdf_text is None:
        print(f"[FAIL] Could not read contributor RDF for {uid} (not found or not authorized).")
        sys.exit(2)
    if archived is None:
        print(f"[FAIL] Contributor {uid} has no jfs:archived property; refusing to guess.")
        sys.exit(2)
    if not archived:
        print(f"[SKIP] {uid} is already active (jfs:archived=false). Nothing to do.")
        return

    print("Current    : jfs:archived=true (ARCHIVED)")
    new_rdf = set_archived_false(rdf_text)
    if new_rdf is None:
        print("[FAIL] Could not rewrite jfs:archived in the RDF.")
        sys.exit(2)

    if not args.commit:
        print("DRY RUN - would PUT jfs:archived=false to reactivate this user. Nothing written.")
        print("Re-run with --commit to apply.")
        return

    if not alm_config.confirm_prod_write(f"Reactivating JTS contributor {uid}"):
        sys.exit(1)
    ok, msg = unarchive(session, server, uid, etag, new_rdf)
    if not ok:
        print(f"[FAIL] Unarchive PUT failed: {msg}")
        sys.exit(3)

    _, _, now_archived = get_contributor(session, server, uid)
    if now_archived is False:
        print(f"[OK] {uid} unarchived - jfs:archived is now false (verified).")
    else:
        print(f"[WARN] PUT reported success ({msg}) but re-read shows archived={now_archived}.")


if __name__ == "__main__":
    main()
