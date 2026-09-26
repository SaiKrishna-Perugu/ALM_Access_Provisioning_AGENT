"""Drive the GPT (Global Provisioning Tool) JSF UI, unattended.

This is the one step that cannot move to Linux: GPT authenticates with Windows
Kerberos SSO, which only flows from a real browser process running as a
domain principal. The worker therefore runs on a domain-joined Windows host as a
gMSA or service account, and launches its own browser with the Negotiate
allowlist set - there is no ``start-gpt.ps1`` and no human logging in.

What this module will not do is claim more than it knows. GPT queues the AD
change (it appears under Pending Requests) and clicking Modify clears the
staging grid, so group membership cannot be confirmed synchronously. The
previous implementation re-read that cleared grid and reported ten false
failures while GPT itself reported zero. The only honest synchronous signal is
GPT's own confirmation, and the JazzUsers permission poll in the orchestrator is
what actually proves the access landed.
"""
from __future__ import annotations

import re
import time

from alm_core.logging import get_logger

log = get_logger("alm.worker.gpt")

DEFAULT_URL = "https://gpt.fiatspa.com/GlobalProvisioningTool/home.jsf"


def parse_submit_body(body: str) -> tuple[bool, str]:
    """Interpret GPT's page text after Modify. Pure, so it can be reasoned about.

    A page that reports a failure count is believed on the number; a page with
    neither a count nor the confirmation sentence is *not* treated as success.
    Silence is not consent.
    """
    body = " ".join((body or "").split())
    match = re.search(r"Failed Requests:\s*(\d+)", body)
    if match:
        return int(match.group(1)) == 0, body[:200]
    return "submitted correctly" in body.lower(), body[:200]


class GptSession:
    """A browser attached to GPT, reused across jobs.

    Reused deliberately: signing in to GPT and navigating the JSF menus costs
    several seconds, and the queue is expected to deliver bursts.
    """

    def __init__(self, *, url: str = DEFAULT_URL, ad_label: str = "inetpsa.com",
                 profile_dir: str = "", headless: bool = True):
        self.url = url
        self.ad_label = ad_label
        self.profile_dir = profile_dir or "C:\\ProgramData\\alm-worker\\chrome-profile"
        self.headless = headless
        self._playwright = None
        self._context = None
        self._page = None
        self._browser = None      # set only when attached to an existing browser

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        host = self.url.split("/")[2]
        self._playwright = sync_playwright().start()
        # A fresh user-data-dir carries no SSO policy, so Negotiate has to be
        # allowed explicitly for the GPT host or navigation fails with
        # ERR_INVALID_AUTH_CREDENTIALS.
        self._context = self._playwright.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            args=[
                f"--auth-server-allowlist=*{host}",
                f"--auth-negotiate-delegate-allowlist=*{host}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._page = self._context.pages[0] if self._context.pages \
            else self._context.new_page()
        log.info("gpt_browser_started", host=host, headless=self.headless)

    def attach(self, cdp_url: str) -> None:
        """Use a browser someone already signed in to GPT, instead of launching one.

        The local counterpart of :meth:`start`, and the path the CLI has always
        used in production: ``scripts/start-gpt.ps1`` opens a debug Chrome, the
        operator signs in to GPT once, and this attaches over CDP. The session
        opens a tab of its own and never touches the operator's tabs.
        """
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise
        # start-gpt.ps1 opens an Incognito window, which is a separate context;
        # take the one that actually has a window, as elm_gpt.py does.
        contexts = self._browser.contexts
        context = next((c for c in contexts if c.pages), contexts[0] if contexts else None)
        if context is None:
            context = self._browser.new_context()
        self._page = context.new_page()
        log.info("gpt_browser_attached", cdp=cdp_url)

    def close(self) -> None:
        try:
            if self._browser is not None:
                # Attached: close only our tab, then disconnect. The operator's
                # browser and sign-in stay as they were.
                if self._page is not None:
                    self._page.close()
                self._browser.close()
            elif self._context is not None:
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._context = self._page = self._playwright = self._browser = None

    # ------------------------------------------------------------ navigation

    def _assert_authenticated(self) -> None:
        text = (self._page.inner_text("body") or "").lower()
        if "do not have permission" in text:
            raise RuntimeError(
                "GPT returned 401: Kerberos did not flow. Check that the worker "
                "runs as the domain service account and that the SPN is correct.")

    def open_group(self, group: str) -> None:
        """Navigate to Modify Membership for one AD group."""
        page = self._page
        # The JSF URL never changes, so state carries over between jobs unless
        # the view is reset explicitly.
        page.goto(self.url, wait_until="domcontentloaded")
        self._assert_authenticated()

        # "Modify Membership" is a span.shoulderLink with a generated id that
        # shifts between renders, and it stays hidden until the menu opens, so
        # its JSF click handler is fired directly.
        page.get_by_text("Groups Management", exact=True).first.click()
        time.sleep(1)
        page.get_by_text("Modify Membership", exact=True).first.dispatch_event("click")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#searchForm\\:inputActiveDirectory", timeout=20000)
        time.sleep(1)

        page.select_option("#searchForm\\:inputActiveDirectory", label=self.ad_label)
        page.wait_for_load_state("networkidle")
        page.locator("select").nth(1).select_option(label="Equals")
        page.fill("#searchForm\\:inputGroupSearch", group)
        page.locator("input[value='Search']").first.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        page.get_by_role("row", name=group).get_by_text("Select", exact=True).first.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1)

    def stage_user(self, userid: str, domain: str) -> bool:
        """Add one user to the staging grid. Returns whether it appeared."""
        page = self._page
        page.fill("#userComputerSearchResultButtonForm\\:inputUserIdToAssign",
                  f"{domain}\\{userid}")
        page.locator(
            "input[name^='userComputerSearchResultButtonForm'][value='Add']").click()
        page.wait_for_load_state("networkidle")
        for _ in range(10):  # the grid refresh lags behind the request
            if userid in (page.inner_text("body") or ""):
                return True
            time.sleep(0.5)
        return False

    def submit(self) -> tuple[bool, str]:
        """Click Modify and read GPT's own confirmation."""
        page = self._page
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("input[value='Modify']").click()
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        return parse_submit_body(page.inner_text("body") or "")

    # ---------------------------------------------------------------- public

    def add_member(self, *, userid: str, group: str, domain: str) -> tuple[bool, str]:
        """Add one user to one group. Returns ``(submitted, message)``.

        ``submitted`` means GPT accepted the request, not that the user is in
        the group - AD provisioning is asynchronous and confirmed elsewhere.
        """
        self.open_group(group)
        if not self.stage_user(userid, domain):
            return False, f"{userid} did not appear in the staging grid"
        ok, message = self.submit()
        if ok:
            return True, "GPT accepted the request; AD provisioning is queued"
        return False, f"GPT rejected the request: {message}"
