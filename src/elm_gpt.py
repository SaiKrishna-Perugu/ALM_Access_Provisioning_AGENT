"""GPT (Global Provisioning Tool) provisioner for GR_D-JazzUser-NA.

Attaches to a debug-enabled Chrome over CDP (chrome --remote-debugging-port=9222),
which keeps the user's Windows Kerberos ticket so GPT authenticates. Adds the
requested user IDs to the group one after another, then clicks Modify only when
--commit is passed.

ID source precedence: --auto (scrape the open ELM tab) > --users-in JSON from
the retrieval agent (default out/alm_users.json) > USER_IDS from .env.

Usage:
  python src/elm_gpt.py                  # IDs from out/alm_users.json (dry run)
  python src/elm_gpt.py --commit         # add + submit (live)
  python src/elm_gpt.py --auto --commit  # scrape IDs from the open ELM tab
"""
import argparse
import json
import os
import re
import socket
import sys
import time
from urllib.parse import urlparse

from dotenv import load_dotenv

import alm_config
import alm_log
import audit

load_dotenv()
GPT_URL = alm_config.env_or("GPT_URL", "https://gpt.example.intra/GlobalProvisioningTool/home.jsf")
GROUP_NAME = alm_config.env_or("GROUP_NAME", "GR_D-JazzUser-NA")
DOMAIN = alm_config.env_or("DOMAIN", "INETPSA")
AD_LABEL = alm_config.env_or("AD_LABEL", "inetpsa.com")
USER_IDS = [s.strip() for s in os.getenv("USER_IDS", "").split(",") if s.strip()]
USERS_IN_DEFAULT = os.getenv("ALM_USERS_OUT", "out/alm_users.json")
# 127.0.0.1, not localhost: localhost resolves to ::1 first and Chrome listens on IPv4.
CDP = alm_config.env_or("CDP_URL", "http://127.0.0.1:9222")
COMMIT_ENV = os.getenv("COMMIT", "false").lower() == "true"


def cdp_reachable(url, timeout=3.0):
    """True when the debug Chrome is listening. Raw socket: never goes via the proxy."""
    p = urlparse(url)
    try:
        with socket.create_connection((p.hostname or "127.0.0.1", p.port or 9222), timeout):
            return True
    except OSError:
        return False


def ids_from_file(path):
    """User IDs from the retrieval agent's JSON (out/alm_users.json), or []."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    users = data.get("users", []) if isinstance(data, dict) else data
    seen = []
    for u in users:
        uid = (u.get("userid") or "").strip()
        if uid and uid not in seen:
            seen.append(uid)
    if seen:
        print(f"[file] {len(seen)} user ID(s) from {path}: {','.join(seen)}")
    return seen


def extract_ids(b):
    """Read user IDs from any open ELM tab: New Users = LASTNAME,FIRSTNAME,email,USERID;"""
    for ctx in b.contexts:
        for pg in ctx.pages:
            if "ccm/web" not in pg.url:
                continue
            body = pg.inner_text("body") or ""
            ids = re.findall(r"[^,;]+,[^,;]+,[^@,;]+@[^,;]+,([A-Za-z0-9]{6,8});", body)
            if ids:
                seen = []
                for i in ids:
                    if i not in seen:
                        seen.append(i)
                print("[elm] extracted:", ",".join(seen))
                return seen
    print("[elm] no IDs found (open the access-request query first)")
    return []


def assert_auth(pg):
    if "do not have permission" in (pg.inner_text("body") or "").lower():
        raise SystemExit("GPT 401: no Kerberos. Start debug Chrome first (start-gpt.ps1).")
    print("[gpt] authenticated")


def open_modify_membership(pg):
    # Always reset to a clean home view (the JSF URL never changes, so state would
    # otherwise carry over between runs and break the search form).
    pg.goto(GPT_URL, wait_until="domcontentloaded")
    assert_auth(pg)
    # "Groups Management" expands a dropdown; "Modify Membership" is a span.shoulderLink
    # (auto-generated JSF ids shift, and the item stays hidden until the menu is open,
    # so fire its JSF click handler directly via dispatch_event).
    pg.get_by_text("Groups Management", exact=True).first.click()
    time.sleep(1)
    pg.get_by_text("Modify Membership", exact=True).first.dispatch_event("click")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_selector("#searchForm\\:inputActiveDirectory", timeout=20000)
    time.sleep(1)


def select_group(pg):
    pg.select_option("#searchForm\\:inputActiveDirectory", label=AD_LABEL)
    pg.wait_for_load_state("networkidle")
    pg.locator("select").nth(1).select_option(label="Equals")  # match-type dropdown
    pg.fill("#searchForm\\:inputGroupSearch", GROUP_NAME)
    pg.locator("input[value='Search']").first.click()
    pg.wait_for_load_state("networkidle")
    time.sleep(1)
    pg.get_by_role("row", name=GROUP_NAME).get_by_text("Select", exact=True).first.click()
    pg.wait_for_load_state("networkidle")
    time.sleep(1)


def add_user(pg, uid):
    """Stage one user ID. Returns (ok, error); never raises so one bad ID cannot abort the run."""
    try:
        pg.fill("#userComputerSearchResultButtonForm\\:inputUserIdToAssign", f"{DOMAIN}\\{uid}")
        pg.locator("input[name^='userComputerSearchResultButtonForm'][value='Add']").click()
        pg.wait_for_load_state("networkidle")
    except Exception as err:  # noqa: BLE001 - keep going with the remaining IDs
        print(f"[gpt] staged {uid}: ERROR - {err}")
        return False, err
    ok = False
    for _ in range(10):  # grid refresh can lag; wait for the uid to appear
        if uid in (pg.inner_text("body") or ""):
            ok = True
            break
        time.sleep(0.5)
    print(f"[gpt] staged {uid}: {'OK' if ok else 'NOT FOUND'}")
    return ok, None


def parse_submit_body(body: str):
    """Interpret GPT's post-Modify page text; returns (ok, message).

    Pure so it can be tested against captured page text. The earlier
    verify_members() re-read the staging grid that Modify had just cleared and
    reported 10 false failures while GPT itself said "Failed Requests: 0"; GPT's
    own confirmation is the only signal available synchronously, because the AD
    change is queued.
    """
    body = " ".join((body or "").split())
    m = re.search(r"Failed Requests:\s*(\d+)", body)
    failed = int(m.group(1)) if m else None
    if failed is not None:
        return failed == 0, body[:160]
    return "submitted correctly" in body.lower(), body[:160]


def submit_result(pg):
    """Read GPT's own confirmation after Modify; returns (ok, message)."""
    return parse_submit_body(pg.inner_text("body") or "")


def provision(pg, ids, commit):
    """Stage every ID, submit when committing, then confirm the group really contains them."""
    try:
        open_modify_membership(pg)
        select_group(pg)
    except Exception as err:  # noqa: BLE001 - step-level failure, before any user
        # The page never opened, so no user was attempted. Recording these as
        # per-user failures is what made the audit blame users the run never
        # touched; the step failure is recorded once, against the step.
        audit.record("gpt", "", audit.FAILED, outcome="gpt_unreachable", exc=err,
                     message="could not open the group membership page")
        audit.record_not_attempted("gpt", ids, "GPT group membership page did not open")
        print(f"[gpt] FAIL: cannot open the group membership page - {err}")
        if "ERR_INVALID_AUTH_CREDENTIALS" in str(err):
            print("[gpt] GPT rejected the browser credentials. Open the debug Chrome window "
                  "and sign in to GPT, then re-run.")
        return 1

    staged = []
    for uid in ids:
        ok, err = add_user(pg, uid)
        if ok:
            staged.append(uid)
        else:
            audit.record("gpt", uid, "failed", outcome="stage_failed",
                         message="user ID did not appear in the staging grid", exc=err)

    if not commit:
        for uid in staged:
            audit.record("gpt", uid, audit.SKIPPED, outcome="staged",
                         message="dry run - staged only, not submitted")
        print(f"[gpt] DRY RUN - staged {staged}; rerun with --commit to submit")
        return 0

    if not staged:
        print("[gpt] nothing staged - nothing to submit.")
        return 0

    if not alm_config.confirm_prod_write(
            f"Adding {len(staged)} user(s) to AD group {GROUP_NAME}"):
        audit.record_not_attempted("gpt", staged, "production write not confirmed")
        return 1

    try:
        pg.once("dialog", lambda d: d.accept())
        pg.locator("input[value='Modify']").click()
        pg.wait_for_load_state("networkidle")
        time.sleep(2)
        print("[gpt] COMMIT:", pg.inner_text("body")[:120])
        ok, msg = submit_result(pg)
    except Exception as err:  # noqa: BLE001 - a submit failure is step-level
        for uid in staged:
            audit.record("gpt", uid, "failed", outcome="submit_failed", exc=err)
        print(f"[gpt] FAIL submit/verify: {err}")
        return 1

    for uid in staged:
        if ok:
            # GPT queues the AD change asynchronously, so "submitted" is the
            # strongest claim available here. The JazzUsers permission poll in
            # step 4 is what actually confirms the access landed.
            audit.record("gpt", uid, audit.OK, outcome="submitted",
                         message="GPT accepted the request; AD provisioning is queued")
        else:
            audit.record("gpt", uid, audit.FAILED, outcome="submit_rejected", message=msg)
    alm_log.event("gpt_submit", submitted=ok, users=staged, group=GROUP_NAME)
    print(f"[gpt] {'submitted' if ok else 'REJECTED'} {len(staged)} user(s) for {GROUP_NAME}"
          + ("" if ok else f" - {msg}"))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Provision user IDs into the GPT AD group.")
    ap.add_argument("--users-in", default=USERS_IN_DEFAULT,
                    help=f"User IDs JSON from the retrieval agent (default {USERS_IN_DEFAULT}).")
    ap.add_argument("--auto", action="store_true", help="Scrape IDs from the open ELM tab instead.")
    ap.add_argument("--commit", action="store_true", default=COMMIT_ENV,
                    help="Click Modify to submit (else dry run).")
    args = ap.parse_args()

    alm_config.print_banner(f"gpt (AD group {GROUP_NAME})", commit=args.commit)

    if not cdp_reachable(CDP):
        raise SystemExit(f"[STOP] No debug Chrome on {CDP}. Start it first: .\\scripts\\start-gpt.ps1")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ids = extract_ids(b) if args.auto else (ids_from_file(args.users_in) or USER_IDS)
        if not ids:
            raise SystemExit("No user IDs (run the retrieval agent first, use --auto with "
                             "ELM open, or set USER_IDS in .env)")
        # Incognito is a separate context, so pick the one that actually has the window.
        ctx = next((c for c in b.contexts if c.pages), b.contexts[0])
        try:
            rc = provision(ctx.pages[0] if ctx.pages else ctx.new_page(), ids, args.commit)
        finally:
            audit.flush("gpt")
    sys.exit(rc)
