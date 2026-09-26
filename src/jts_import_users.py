"""Import LDAP users into the Jazz Team Server (JTS) user registry.

Reads the user IDs stored by the alm-access-retrieval agent (out/alm_users.json)
and creates JTS repository contributors for them via the SAME REST calls the JTS
admin "Import Users" page uses:

  1. POST .../IExternalUserRegistryRestService/searchRegistry
        body: searchText=<id>&hideExistingUsers=true|false
        -> looks the user up in LDAP (authoritative name / email / validity)
  2. POST .../IAdminRestService/multipleNewContributors
        body: jsonUserInfo=[{"name":..,"userId":..,"emailAddress":..}]
        -> creates the repository contributor from the LDAP entry.
           The Jazz User license / role is assigned automatically by the server.

Dry run (default) only lists the users from the JSON file - no password is
requested and nothing is written. Use --commit to authenticate and import.
"""
import argparse
import getpass
import json
import os
import sys

import requests
from dotenv import load_dotenv

import alm_config
import alm_log
import audit
import jazz_client

load_dotenv()

JTS_SERVER = os.getenv("JTS_SERVER", "https://jts.example.intra/jts").rstrip("/")
USERS_IN_DEFAULT = os.getenv("ALM_USERS_OUT", "out/alm_users.json")
CID = os.getenv("CID", "")
COMMIT_ENV = os.getenv("COMMIT", "false").strip().lower() == "true"

SEARCH_PATH = "/service/com.ibm.team.repository.service.internal.IExternalUserRegistryRestService/searchRegistry"
CREATE_PATH = "/service/com.ibm.team.repository.service.internal.IAdminRestService/multipleNewContributors"

# Headers the JTS web client sends on these service calls.
XHR_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "X-com-ibm-team-configuration-versions": "LATEST",
    "Accept": "text/json",
}


def _whoami_confirms(session, server):
    """JTS's own proof of a live session: /whoami names a contributor resource.

    Distinct from EWM's check (OSLC XML) on purpose - each service is verified by
    a signal only that service can produce.
    """
    who = session.get(f"{server}/whoami", headers={"Accept": "text/plain"},
                      timeout=jazz_client.DEFAULT_TIMEOUT)
    if who.status_code != 200:
        return None
    body = who.text.strip()
    return body if body.startswith("http") and "/users/" in body else None


def login(session, user, password, server):
    """Authenticate to JTS using Jazz form auth. Returns True on success."""
    jazz_client.harden(session)
    identity = None

    def verify(sess):
        nonlocal identity
        identity = _whoami_confirms(sess, server)
        return identity is not None

    ok = jazz_client.form_login(session, server, user, password, verify)
    if ok:
        alm_log.say(f"[OK] Authenticated with JTS as {identity.rsplit('/', 1)[-1]}.",
                    "jts_login_ok", server=server)
    else:
        alm_log.warn("[FAIL] JTS authentication failed. Are you on the Chrysler intranet / VPN?",
                     "jts_login_failed", server=server)
    return ok


def load_users(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    users = data.get("users", []) if isinstance(data, dict) else data
    return users


def _external_users(payload):
    """Pull the externalUsers list out of the searchRegistry SOAP/JSON envelope."""
    try:
        value = payload["soapenv:Body"]["response"]["returnValue"]["value"]
    except (KeyError, TypeError):
        return []
    return value.get("externalUsers", []) or []


def search_registry(session, server, userid, hide_existing):
    """POST searchRegistry; return list of matching LDAP entries (dicts)."""
    body = {"searchText": userid, "hideExistingUsers": "true" if hide_existing else "false"}
    r = session.post(f"{server}{SEARCH_PATH}", data=body, headers=XHR_HEADERS,
                     timeout=jazz_client.DEFAULT_TIMEOUT)
    r.raise_for_status()
    out = []
    for eu in _external_users(r.json()):
        ids = eu.get("userIds") or []
        if userid in ids:  # User ID is case sensitive in JTS.
            out.append(
                {
                    "userId": userid,
                    "name": (eu.get("fullNames") or [userid])[0],
                    "email": (eu.get("emailAddresses") or [""])[0],
                    "valid": bool((eu.get("status") or {}).get("valid", False)),
                }
            )
    return out


def resolve_user(session, server, userid):
    """Classify a user: READY (importable), EXISTS (already imported), or MISSING."""
    ready = search_registry(session, server, userid, hide_existing=True)
    if ready:
        info = ready[0]
        return ("READY", info) if info["valid"] else ("INVALID", info)
    allmatch = search_registry(session, server, userid, hide_existing=False)
    if allmatch:
        return ("EXISTS", allmatch[0])
    return ("MISSING", None)


def create_contributor(session, server, info):
    """POST multipleNewContributors for a single resolved LDAP user."""
    payload = [{"name": info["name"], "userId": info["userId"], "emailAddress": info["email"]}]
    body = {"jsonUserInfo": json.dumps(payload)}
    r = session.post(f"{server}{CREATE_PATH}", data=body, headers=XHR_HEADERS,
                     timeout=jazz_client.DEFAULT_TIMEOUT)
    if r.status_code not in (200, 201):
        return False, f"HTTP {r.status_code} - {r.text[:200]}"
    txt = r.text
    # A successful call returns a SOAP/JSON "response" envelope; a server fault
    # comes back as a SOAP fault or a stack trace instead.
    if "soapenv:Fault" in txt or "stackTrace" in txt or '"response"' not in txt:
        return False, f"server error - {txt[:200]}"
    return True, "created"


def verify_active(session, server, uid):
    """Re-read the contributor after a write to confirm it exists and is not archived."""
    import jts_unarchive_user as unarch

    _, rdf_text, archived = unarch.get_contributor(session, server, uid)
    if rdf_text is None:
        return False, "contributor not readable after write"
    if archived:
        return False, "contributor still archived after write"
    return True, "active"


def main() -> int:
    """Exit codes: 0 all good, 1 bad input, 2 auth, 3 some users failed, 4 all failed."""
    ap = argparse.ArgumentParser(description="Import LDAP users into JTS.")
    ap.add_argument("--users-in", default=USERS_IN_DEFAULT, help="Input JSON (default out/alm_users.json).")
    ap.add_argument("--server", default=JTS_SERVER, help="JTS base URL.")
    ap.add_argument("--user", default=CID, help="JTS admin/CID username (default from .env CID).")
    ap.add_argument("--commit", action="store_true", default=COMMIT_ENV, help="Actually import (else dry run).")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N users (0 = all).")
    ap.add_argument("--no-unarchive", action="store_true", help="Do not reactivate archived existing users.")
    args = ap.parse_args()

    server = args.server.rstrip("/")

    users = load_users(args.users_in)
    if users is None:
        print(f"[STOP] Input file not found: {args.users_in}")
        print("       Run the alm-access-retrieval agent first to generate it.")
        return 1
    if not users:
        print(f"[STOP] No users in {args.users_in}. Run the alm-access-retrieval agent first.")
        return 1

    if args.limit > 0:
        users = users[: args.limit]

    alm_config.print_banner("import (JTS registry)", commit=args.commit)
    print(f"Input      : {args.users_in}")
    print(f"JTS server : {server}")
    print(f"Users      : {len(users)}")
    print()

    if not args.commit:
        print("DRY RUN - the following users WOULD be imported into JTS (nothing written):")
        print(f"  {'USERID':<10} {'NAME':<32} EMAIL")
        for u in users:
            name = " ".join(x for x in [u.get("first_name", ""), u.get("last_name", "")] if x).strip() or "-"
            print(f"  {u.get('userid', '?'):<10} {name[:32]:<32} {u.get('email', '-')}")
            if u.get("userid"):
                audit.record("import", u["userid"], "skipped", outcome="dry_run",
                             message="would be imported")
        audit.flush("import")
        print()
        print("Re-run with --commit to authenticate and import these users.")
        return 0

    if not args.user:
        print("[STOP] No username. Set CID in .env or pass --user.")
        return 1

    if not alm_config.confirm_prod_write(f"Importing {len(users)} user(s) into JTS"):
        audit.record_not_attempted("import", [u.get("userid", "") for u in users if u.get("userid")],
                                   "production write not confirmed")
        audit.flush("import")
        return 1

    # EWM_PASSWORD lets the pipeline orchestrator prompt once for all steps.
    password = os.getenv("EWM_PASSWORD") or getpass.getpass(f"JTS password for {args.user}: ")
    session = jazz_client.make_session()
    if not login(session, args.user, password, server):
        return 2

    # Lazy import avoids a circular import at module load (the unarchive module
    # imports this one for its auth/registry helpers).
    import jts_unarchive_user as unarch

    created = skipped = failed = unarchived = 0
    for u in users:
        uid = u.get("userid", "")
        if not uid:
            continue
        try:
            status, info = resolve_user(session, server, uid)
        except requests.RequestException as e:
            print(f"[FAIL] {uid}: search error - {e}")
            audit.record("import", uid, "failed", outcome="search_error", exc=e)
            failed += 1
            continue

        try:
            if status == "EXISTS":
                # Already a JTS contributor - but it may be archived. Reactivate it so
                # the access request is actually fulfilled (unless --no-unarchive).
                etag, rdf_text, archived = unarch.get_contributor(session, server, uid)
                if archived and not args.no_unarchive:
                    new_rdf = unarch.set_archived_false(rdf_text)
                    ok, msg = unarch.unarchive(session, server, uid, etag, new_rdf)
                    if ok:
                        ok, msg = verify_active(session, server, uid)
                    if ok:
                        print(f"[UNARCH] {uid}: already a JTS user but ARCHIVED - reactivated.")
                        audit.record("import", uid, "ok", outcome="unarchived",
                                     message="verified active")
                        unarchived += 1
                    else:
                        print(f"[FAIL] {uid}: archived and unarchive failed - {msg}")
                        audit.record("import", uid, "failed", outcome="unarchive_failed",
                                     message=msg)
                        failed += 1
                elif archived:
                    print(f"[SKIP] {uid}: already a JTS user but ARCHIVED (--no-unarchive set).")
                    audit.record("import", uid, "skipped", outcome="archived_not_reactivated",
                                 message="--no-unarchive set")
                    skipped += 1
                else:
                    print(f"[SKIP] {uid}: already a JTS user (active).")
                    audit.record("import", uid, "ok", outcome="already_active")
                    skipped += 1
                continue
            if status == "MISSING":
                print(f"[SKIP] {uid}: not found in LDAP.")
                audit.record("import", uid, "failed", outcome="not_in_ldap",
                             message="no LDAP entry for this user ID")
                skipped += 1
                continue
            if status == "INVALID":
                print(f"[SKIP] {uid}: LDAP entry marked invalid.")
                audit.record("import", uid, "failed", outcome="ldap_invalid",
                             message="LDAP entry marked invalid")
                skipped += 1
                continue

            ok, msg = create_contributor(session, server, info)
            if ok:
                ok, msg = verify_active(session, server, uid)
            if ok:
                print(f"[OK]   {uid}: {info['name']} <{info['email']}>")
                audit.record("import", uid, "ok", outcome="created", message="verified active")
                created += 1
            else:
                print(f"[FAIL] {uid}: {msg}")
                audit.record("import", uid, "failed", outcome="create_failed", message=msg)
                failed += 1
        except Exception as e:  # noqa: BLE001 - one bad user must not stop the rest
            print(f"[FAIL] {uid}: {e}")
            audit.record("import", uid, "failed", outcome="unexpected_error", exc=e)
            failed += 1

    audit.flush("import")
    print()
    print(
        f"Summary: {created} created, {unarchived} unarchived, {skipped} skipped, "
        f"{failed} failed (of {len(users)})."
    )
    alm_log.event("import_summary", created=created, unarchived=unarchived,
                  skipped=skipped, failed=failed, total=len(users))

    # An exit code an external scheduler can trust. This used to be 0 even when
    # every single user failed, which made the status meaningless; the pipeline
    # orchestrator knows 3 means "partial" and carries on with the users that
    # did succeed.
    if failed == 0:
        return 0
    return 4 if created + unarchived + skipped == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
