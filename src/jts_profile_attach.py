"""Screenshot each imported user's JTS profile and attach it to their work items.

For every user in out/alm_users.json, opens the JTS contributor page
({JTS_SERVER}/users/<USERID>) in a headless Edge, saves a full-page screenshot
to out/screenshots/<USERID>.png, then attaches each screenshot to the ALM
Access Request work item(s) that requested that user.

Dry run (default) takes the screenshots and prints the attach plan without
touching EWM. Use --commit to upload the attachments.

Uses the installed Microsoft Edge (channel=msedge), so no Playwright browser
download is needed. Config (CID, EWM_SERVER, JTS_SERVER, ALM_USERS_OUT) comes
from .env; the password is prompted at runtime and never stored.

Usage:  python src/jts_profile_attach.py [--workitem ID] [--skip-shots] [--commit]
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

import requests
import urllib3

import alm_access_requests as aar
import alm_config
import alm_log
import audit
import evidence
import idempotency
import jazz_client
import jts_import_users as jimp
from ewm_comment_workitems import group_by_workitem

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SHOTS_DIR_DEFAULT = "out/screenshots"
LOGIN_USER_SEL = "input[name='j_username']"
# The profile's User ID is a read-only <input> value, so it appears in neither
# inner_text() nor content() - matching an input value is the only reliable proof
# that the page really shows the requested user.
_UID_IN_INPUTS = """uid => Array.from(document.querySelectorAll('input'))
    .some(e => (e.value || '').trim().toLowerCase() === uid.toLowerCase())"""


def _on_login_page(pg) -> bool:
    """True when the Jazz auth form is showing (it is built by Dojo after load)."""
    return "/auth/" in pg.url or pg.locator(LOGIN_USER_SEL).count() > 0


def take_screenshots(jts: str, user: str, password: str, userids: list[str],
                     shots_dir: str, headed: bool) -> dict[str, str]:
    """Log in to JTS in Edge and save a full-page profile screenshot per user ID.

    Users whose profile cannot be confirmed are skipped, not captured.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(shots_dir, exist_ok=True)
    paths: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=not headed)
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1400, "height": 1000})
        pg = ctx.new_page()
        try:
            pg.goto(f"{jts}/users/{user}", wait_until="networkidle")
            # The login widget is rendered by Dojo, so it is absent at domcontentloaded.
            try:
                pg.wait_for_selector(LOGIN_USER_SEL, timeout=15000)
            except Exception:  # noqa: BLE001 - no form means the session is already valid
                pass
            if pg.locator(LOGIN_USER_SEL).count():
                pg.fill(LOGIN_USER_SEL, user)
                pg.fill("input[name='j_password']", password)
                pg.locator("input[name='j_password']").press("Enter")
                pg.wait_for_load_state("networkidle")
                pg.wait_for_timeout(1500)
            if _on_login_page(pg):
                raise RuntimeError(
                    f"JTS login failed in the browser (still at {pg.url}). Check password / VPN.")
            print(f"[OK] Browser authenticated with JTS as {user}.")

            for uid in userids:
                try:
                    pg.goto(f"{jts}/users/{uid}", wait_until="networkidle")
                    if _on_login_page(pg):
                        raise RuntimeError("profile redirected to the login page - not authenticated")
                    # Wait for the profile itself, not a fixed delay.
                    pg.wait_for_function(_UID_IN_INPUTS, arg=uid, timeout=20000)
                except Exception as err:  # noqa: BLE001 - skip this user, keep the rest
                    print(f"[FAIL] {uid}: profile not confirmed, no screenshot taken "
                          f"({str(err).splitlines()[0]})")
                    continue
                path = os.path.join(shots_dir, f"{uid}.png")
                pg.screenshot(path=path, full_page=True)
                print(f"[OK] {uid}: screenshot -> {path}")
                paths[uid] = path
        finally:
            browser.close()
    return paths


ATTACH_PROP = "rtc_cm:com.ibm.team.workitem.linktype.attachment.attachment"
UPLOAD_PATH = "/service/com.ibm.team.workitem.service.internal.rest.IAttachmentRestService"


def upload_attachment(session, ewm_server: str, project_uuid: str, path: str, filename: str) -> str:
    """Upload a file via the web UI's attachment service; returns the attachment URL.

    The OSLC attachment factory returns 415 on this server, so this uses the same
    internal multipart upload the EWM web client performs. No category param: it
    must be a category UUID, and omitting it yields 'Unassigned'.
    """
    url = f"{ewm_server}{UPLOAD_PATH}?projectId={project_uuid}&multiple=true"
    headers = {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "X-Jazz-CSRF-Prevent": session.cookies.get("JSESSIONID") or "",
    }
    with open(path, "rb") as fh:
        r = session.post(url, files={"attach": (filename, fh.read(), "image/png")},
                         headers=headers, timeout=(15, 120))
    if r.status_code != 200:
        raise RuntimeError(f"upload failed: HTTP {r.status_code} - {r.text[:200]}")
    # Response JSON is wrapped in <html><body><textarea>...</textarea></body></html>.
    body = r.text
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"unexpected upload response: {body[:200]}")
    files = json.loads(body[start:end + 1]).get("files", [])
    if not files or "url" not in files[0]:
        raise RuntimeError(f"upload response has no file URL: {body[:300]}")
    return files[0]["url"]


def existing_attachment_titles(session, ewm_server: str, wid: str) -> list[str] | None:
    """Filenames already attached to a work item, or None when unreadable.

    ``link_attachment`` only ever compared attachment URLs, and a re-upload
    produces a new URL every time, so its "already linked" guard could never fire
    on a re-run. Comparing filenames is what actually detects a duplicate.
    """
    headers = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}
    try:
        g = session.get(f"{ewm_server}/oslc/workitems/{wid}",
                        params={"oslc.properties": ATTACH_PROP}, headers=headers,
                        timeout=jazz_client.DEFAULT_TIMEOUT)
        if g.status_code != 200:
            return None
        current = g.json().get(ATTACH_PROP, [])
    except (requests.RequestException, ValueError):
        return None
    current = current if isinstance(current, list) else [current]

    titles: list[str] = []
    for item in current[:100]:
        url = item.get("rdf:resource") if isinstance(item, dict) else None
        if not url:
            continue
        try:
            r = session.get(url, headers=headers, timeout=jazz_client.DEFAULT_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                title = data.get("dcterms:title") or data.get("oslc:shortTitle") or ""
                if title:
                    titles.append(str(title))
        except (requests.RequestException, ValueError):
            # One unreadable attachment must not make the whole check unusable,
            # but it does mean the answer is incomplete - say so with None only
            # when nothing at all could be read.
            continue
    return titles


def link_attachment(session, ewm_server: str, wid: str, attachment_url: str):
    """Append the uploaded attachment to the work item via an OSLC partial PUT."""
    wi = f"{ewm_server}/oslc/workitems/{wid}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OSLC-Core-Version": "2.0",
        "X-Jazz-CSRF-Prevent": session.cookies.get("JSESSIONID") or "",
    }
    g = session.get(wi, params={"oslc.properties": ATTACH_PROP}, headers=headers, timeout=(15, 60))
    if g.status_code != 200:
        return False, f"read failed: HTTP {g.status_code}"
    cur = g.json().get(ATTACH_PROP, [])
    cur = cur if isinstance(cur, list) else [cur]
    if any(x.get("rdf:resource") == attachment_url for x in cur):
        return True, "already linked"
    cur.append({"rdf:resource": attachment_url})
    r = session.put(f"{wi}?oslc.properties={ATTACH_PROP}",
                    data=json.dumps({ATTACH_PROP: cur}),
                    headers={**headers, "If-Match": g.headers.get("ETag", "*")},
                    timeout=(15, 60))
    if r.status_code in (200, 204):
        return True, f"HTTP {r.status_code}"
    return False, f"link failed: HTTP {r.status_code} - {r.text[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Screenshot JTS user profiles and attach them to their work items.")
    ap.add_argument("--users-in", default=jimp.USERS_IN_DEFAULT,
                    help="Input JSON (default out/alm_users.json).")
    ap.add_argument("--jts-server", default=jimp.JTS_SERVER, help="JTS base URL.")
    ap.add_argument("--user", default=jimp.CID, help="CID username (default from .env CID).")
    ap.add_argument("--workitem", action="append", default=None, metavar="ID",
                    help="Only process this work item ID (repeatable).")
    ap.add_argument("--limit", type=int, default=0, help="Only the first N work items (0 = all).")
    ap.add_argument("--shots-dir", default=SHOTS_DIR_DEFAULT,
                    help=f"Screenshot folder (default {SHOTS_DIR_DEFAULT}).")
    ap.add_argument("--skip-shots", action="store_true",
                    help="Reuse existing screenshots in --shots-dir instead of taking new ones.")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser window (debugging the JTS login).")
    ap.add_argument("--commit", action="store_true",
                    help="Attach the screenshots to the EWM work items (else dry run).")
    args = ap.parse_args()

    if not args.user:
        print("[STOP] No username. Set CID in .env or pass --user.")
        return 1

    users = jimp.load_users(args.users_in)
    if not users:
        print(f"[STOP] No users in {args.users_in}. Run the alm-access-retrieval agent first.")
        return 1

    order, wi_users, _ = group_by_workitem(users)
    if args.workitem:
        wanted = {w.strip() for w in args.workitem}
        missing = wanted - set(order)
        if missing:
            print(f"[STOP] Work item(s) not found in {args.users_in}: {', '.join(sorted(missing))}")
            return 1
        order = [wid for wid in order if wid in wanted]
    if args.limit > 0:
        order = order[: args.limit]
    unique_ids = sorted({m["userid"] for wid in order for m in wi_users[wid]})

    alm_config.print_banner("attach (evidence)", commit=args.commit)
    print(f"Input      : {args.users_in}")
    print(f"JTS server : {args.jts_server}")
    print(f"EWM server : {aar.SERVER}")
    print(f"Work items : {len(order)}   Users: {len(unique_ids)}")
    print(f"Mode       : {'COMMIT' if args.commit else 'DRY RUN'}")
    print()

    # EWM_PASSWORD lets the pipeline orchestrator prompt once for all steps.
    password = os.getenv("EWM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")

    # 1) Screenshots (read-only on JTS).
    if args.skip_shots:
        shots = {uid: os.path.join(args.shots_dir, f"{uid}.png") for uid in unique_ids}
    else:
        try:
            shots = take_screenshots(args.jts_server.rstrip("/"), args.user, password,
                                     unique_ids, args.shots_dir, args.headed)
        except Exception as err:  # noqa: BLE001
            print(f"\n[ERROR] {err}")
            for uid in unique_ids:
                audit.record("attach", uid, "failed", outcome="screenshot_failed", exc=err)
            audit.flush("attach")
            return 1
    missing_shots = [uid for uid in unique_ids if not os.path.exists(shots.get(uid, ""))]
    for uid in missing_shots:
        audit.record("attach", uid, audit.FAILED, outcome="screenshot_missing",
                     message="profile could not be confirmed - nothing attached for this user")
    if missing_shots:
        print(f"[SKIP] No screenshot for: {', '.join(missing_shots)} - these users get nothing attached.")

    # Evidence gate. Distinctness is checked independently of how the capture
    # step decided it had succeeded: N users must yield N different files. In the
    # 2026-08-27 incident 17 screenshots collapsed to 2 distinct images (the JTS
    # login page) and every one of them was attached and reported as success.
    captured = {uid: path for uid, path in shots.items()
                if uid in unique_ids and uid not in missing_shots}
    ok_evidence, problems = evidence.validate(captured)
    if not ok_evidence:
        print()
        print("[STOP] Evidence validation failed - nothing will be uploaded:")
        for problem in problems:
            print(f"  - {problem}")
        for uid in sorted(captured):
            audit.record("attach", uid, audit.FAILED, outcome="evidence_invalid",
                         message="; ".join(problems)[:500],
                         screenshot=captured.get(uid, ""))
        audit.flush("attach")
        alm_log.event("evidence_gate_failed", level="error", problems=problems,
                      users=sorted(captured))
        return 1
    if captured:
        print(f"[OK] Evidence check: {len(captured)} artifact(s), all distinct.")
    # Keep going for the users that were captured; a bad profile must not block the rest.
    wi_users = {wid: [m for m in members if m["userid"] not in missing_shots]
                for wid, members in wi_users.items()}
    order = [wid for wid in order if wi_users.get(wid)]
    if not order:
        print("[STOP] No confirmed profile screenshots - nothing to attach.")
        audit.flush("attach")
        return 1

    # 2) Attach plan.
    print()
    for wid in order:
        for m in wi_users[wid]:
            print(f"  {wid} <- {shots[m['userid']]}")
    if not args.commit:
        for wid in order:
            for m in wi_users[wid]:
                audit.record("attach", m["userid"], "skipped", outcome="dry_run",
                             message=f"would attach to {wid}", workitem=wid,
                             screenshot=shots[m["userid"]])
        audit.flush("attach")
        print("\nDRY RUN - nothing attached. Re-run with --commit to attach the screenshots.")
        return 0

    if not alm_config.confirm_prod_write(
            f"Attaching evidence to {len(order)} work item(s)"):
        audit.record_not_attempted(
            "attach", sorted({m["userid"] for wid in order for m in wi_users[wid]}),
            "production write not confirmed")
        audit.flush("attach")
        return 1

    # 3) Commit: upload each screenshot and link it to its work item(s).
    try:
        esession = aar.login(args.user, password)
        uuid = aar.project_uuid(esession)
    except Exception as err:  # noqa: BLE001
        print(f"\n[ERROR] {err}")
        if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            print("Ensure you are on the Chrysler intranet / VPN.")
        return 1

    attached = failed = duplicate = 0
    for wid in order:
        # One read per work item, reused for every user on it.
        titles = existing_attachment_titles(esession, aar.SERVER, wid)
        if titles is None:
            alm_log.warn(f"[warn] {wid}: could not read existing attachments; "
                         "uploading without a duplicate check.",
                         "idempotency_unverified", workitem=wid)
        for m in wi_users[wid]:
            uid = m["userid"]
            name = idempotency.attachment_name(uid)
            if titles is not None and idempotency.already_attached(uid, titles):
                print(f"[SKIP] {wid}: {name} already attached - not uploading again.")
                audit.record("attach", uid, audit.SKIPPED, outcome="already_attached",
                             message="attachment with this filename already on the work item",
                             workitem=wid, screenshot=shots[uid])
                duplicate += 1
                continue
            try:
                att_url = upload_attachment(esession, aar.SERVER, uuid, shots[uid], name)
                ok, msg = link_attachment(esession, aar.SERVER, wid, att_url)
            except Exception as err:  # noqa: BLE001
                ok, msg = False, str(err)
            if ok:
                print(f"[OK]   {wid}: attached {name} ({msg}).")
                attached += 1
                if titles is not None:
                    titles.append(name)
            else:
                print(f"[FAIL] {wid}: {name}: {msg}")
                failed += 1
            audit.record("attach", uid, audit.OK if ok else audit.FAILED,
                         outcome="attached" if ok else "attach_failed",
                         message=msg, workitem=wid, screenshot=shots[uid])

    audit.flush("attach")
    print()
    print(f"Summary: {attached} attached, {duplicate} already present, {failed} failed.")
    alm_log.event("attach_summary", attached=attached, duplicate=duplicate, failed=failed)
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
