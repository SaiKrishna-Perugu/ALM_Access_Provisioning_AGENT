"""Evidence capture and the invariant that makes it trustworthy.

On 2026-08-27 seventeen "profile screenshots" were captured, all of them the JTS
login page, and every one was attached to a production work item and reported as
success. The browser step had verified its login using the same expression the
login step used to decide whether to log in, so both agreed and both were wrong.

Two independent signals are required here, and neither can satisfy the other:

1. **Per-artifact** - the page must contain the target user ID in an ``<input>``
   value. The profile's User ID field is read-only, so it appears in neither
   ``inner_text()`` nor ``content()``; matching the input value is the only
   proof the page is that user's profile.
2. **Per-batch** - N users must yield N distinct files. Identical artifacts mean
   the capture mechanism itself is broken, so the whole batch is refused rather
   than the duplicates being dropped: the remaining files cannot be trusted
   either, however plausible they look.
"""
from __future__ import annotations

import hashlib
import os

from ..errors import EvidenceInvalid
from ..logging import get_logger
from .base import ToolContext, to_thread

log = get_logger("alm.tools.evidence")

# A full-page PNG of a real profile is tens of KB. Anything smaller is an error
# page or a blank viewport, whatever its filename says.
MIN_BYTES = 4096
LOGIN_USER_SELECTOR = "input[name='j_username']"

# The User ID is a read-only input value, so this is the only reliable proof
# that the page really shows the requested user.
_UID_IN_INPUTS = """uid => Array.from(document.querySelectorAll('input'))
    .some(e => (e.value || '').trim().toLowerCase() === uid.toLowerCase())"""


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def duplicate_groups(artifacts: dict[str, str]) -> list[tuple[str, list[str]]]:
    """[(digest, [userids sharing it])] for every artifact used more than once."""
    by_digest: dict[str, list[str]] = {}
    for userid in sorted(artifacts):
        path = artifacts[userid]
        if path and os.path.exists(path):
            by_digest.setdefault(file_digest(path), []).append(userid)
    return [(digest, uids) for digest, uids in sorted(by_digest.items()) if len(uids) > 1]


def validate(artifacts: dict[str, str], min_bytes: int = MIN_BYTES) -> list[str]:
    """Return the problems with an artifact set; empty means it may be uploaded."""
    problems: list[str] = []
    for userid in sorted(artifacts):
        path = artifacts[userid]
        if not path or not os.path.exists(path):
            problems.append(f"{userid}: no artifact at {path or '(unset)'}")
            continue
        size = os.path.getsize(path)
        if size < min_bytes:
            problems.append(f"{userid}: artifact is only {size} bytes "
                            f"(minimum {min_bytes}) - not a real profile page")

    for digest, uids in duplicate_groups(artifacts):
        problems.append(
            f"identical artifact shared by {len(uids)} users ({', '.join(uids)}) "
            f"[sha256 {digest[:12]}] - the signature of the login-page-as-evidence "
            "defect; nothing will be uploaded")
    return problems


def require_valid(artifacts: dict[str, str]) -> None:
    """Raise EvidenceInvalid unless the whole batch is fit to upload."""
    problems = validate(artifacts)
    if problems:
        raise EvidenceInvalid("evidence validation failed",
                              context={"problems": problems,
                                       "users": sorted(artifacts)})


def browser_channel() -> str:
    """The browser to capture with.

    ``ALM_BROWSER_CHANNEL`` wins. Otherwise the installed Microsoft Edge on
    Windows - what the CLI's jts_profile_attach.py uses in production, and what
    an operator laptop is sure to have - and Playwright's bundled Chromium
    elsewhere (the Linux container).
    """
    configured = os.getenv("ALM_BROWSER_CHANNEL", "").strip()
    if configured:
        return "" if configured.lower() == "chromium" else configured
    return "msedge" if os.name == "nt" else ""


def browser_headed() -> bool:
    """``ALM_BROWSER_HEADED=true`` shows the capture window, for troubleshooting."""
    return os.getenv("ALM_BROWSER_HEADED", "").strip().lower() in {"1", "true", "yes"}


def _capture(jts_server: str, user: str, password: str, userids: list[str],
             out_dir: str, *, channel: str, verify_tls: bool,
             headless: bool = True) -> dict[str, str]:
    """Log in to JTS in a headless browser and screenshot each profile.

    A user whose profile cannot be confirmed is skipped, never captured. The
    browser channel is configurable because the operator laptop has Edge and the
    Linux container has bundled Chromium.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    captured: dict[str, str] = {}

    with sync_playwright() as playwright:
        launch_kwargs = {"headless": headless}
        if channel:
            launch_kwargs["channel"] = channel
        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context(ignore_https_errors=not verify_tls,
                                      viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        try:
            page.goto(f"{jts_server}/users/{user}", wait_until="networkidle")
            try:
                # The Jazz login widget is built by Dojo, so it is absent at
                # domcontentloaded.
                page.wait_for_selector(LOGIN_USER_SELECTOR, timeout=15000)
            except Exception:  # noqa: S110, BLE001 - no form means the session is valid
                pass
            if page.locator(LOGIN_USER_SELECTOR).count():
                page.fill(LOGIN_USER_SELECTOR, user)
                page.fill("input[name='j_password']", password)
                page.locator("input[name='j_password']").press("Enter")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
            if "/auth/" in page.url or page.locator(LOGIN_USER_SELECTOR).count() > 0:
                raise EvidenceInvalid(
                    f"browser login to JTS failed (still at {page.url})")

            for userid in userids:
                try:
                    page.goto(f"{jts_server}/users/{userid}", wait_until="networkidle")
                    if "/auth/" in page.url:
                        raise EvidenceInvalid("profile redirected to the login page")
                    # Wait for the profile itself, not a fixed delay.
                    page.wait_for_function(_UID_IN_INPUTS, arg=userid, timeout=20000)
                except Exception as err:  # noqa: BLE001 - skip, keep the rest
                    log.warning("profile_not_confirmed", userid=userid,
                                error=str(err).splitlines()[0])
                    continue
                path = os.path.join(out_dir, f"{userid}.png")
                page.screenshot(path=path, full_page=True)
                captured[userid] = path
        finally:
            browser.close()
    return captured


async def capture_profiles(ctx: ToolContext, userids: list[str], out_dir: str,
                           *, channel: str = "") -> dict[str, str]:
    """Capture and validate a batch of profile screenshots.

    Raises :class:`EvidenceInvalid` if the batch fails either check, so a broken
    capture can never reach :func:`alm_core.tools.ewm.attach_evidence`.
    """
    password = ctx.client.resolver.get(ctx.settings.password_secret_name)
    artifacts = await to_thread(
        _capture, ctx.settings.jts_server, ctx.settings.service_account, password,
        userids, out_dir,
        channel=channel or browser_channel(),
        verify_tls=bool(ctx.settings.verify), headless=not browser_headed())

    missing = [u for u in userids if u not in artifacts]
    if missing:
        log.warning("evidence_missing_for_users", users=missing)
    require_valid(artifacts)
    log.info("evidence_validated", count=len(artifacts))
    return artifacts
