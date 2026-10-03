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

import os
import re
import time

from alm_core.logging import get_logger

log = get_logger("alm.worker.gpt")

# `or`, not a getenv default: a blank GPT_URL= (the .env.example shape) must
# fall back rather than become an empty URL.
DEFAULT_URL = ((os.getenv("GPT_URL") or "").strip()
               or "https://gpt.example.intra/GlobalProvisioningTool/home.jsf")


def submit_outcome(body: str) -> tuple[str, str]:
    """"ok", "rejected" or "unknown" for GPT's page after Modify.

    A page with neither a failure count nor the confirmation is "unknown", not
    "rejected": GPT may well have accepted the request, and retrying it would
    add the user twice.
    """
    body = " ".join((body or "").split())
    match = re.search(r"Failed Requests:\s*(\d+)", body)
    if match:
        return ("ok" if int(match.group(1)) == 0 else "rejected"), body[:200]
    if "submitted correctly" in body.lower():
        return "ok", body[:200]
    return "unknown", body[:200]


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

    def click_modify(self) -> str:
        """Click Modify and return GPT's page text. The request is sent here."""
        page = self._page
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("input[value='Modify']").click()
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        return page.inner_text("body") or ""

    # ---------------------------------------------------------------- public

    def add_member(self, *, userid: str, group: str, domain: str) -> tuple[bool, str]:
        """Add one user to one group. Returns ``(submitted, message)``.

        ``submitted`` means GPT accepted the request, not that the user is in
        the group - AD provisioning is asynchronous and confirmed elsewhere.

        A failure *before* Modify is clicked raises as it is: nothing was
        submitted, so a retry is safe. A failure *after* it, or a reply that
        shows neither success nor a failure count, raises ``OutcomeUnknown``:
        GPT may have accepted the request, the ledger closes the write instead
        of retrying it, and a human checks GPT Pending Requests.
        """
        from alm_core import trace
        from alm_core.errors import OutcomeUnknown

        with trace.span("gpt", "open_group", group=group):
            self.open_group(group)
        with trace.span("gpt", "stage_user", userid=userid, domain=domain) as step:
            staged = self.stage_user(userid, domain)
            step["ok"] = bool(staged)
        if not staged:
            return False, f"{userid} did not appear in the staging grid"
        try:
            with trace.span("gpt", "modify", userid=userid, group=group) as step:
                outcome, text = submit_outcome(self.click_modify())
                step.update(ok=outcome == "ok", outcome=outcome, reply=text[:500])
        except Exception as err:
            raise OutcomeUnknown(
                f"GPT may or may not have accepted {userid} for {group}: the page "
                f"failed after Modify was clicked ({type(err).__name__}). Check GPT "
                "Pending Requests before doing anything; this will not be retried "
                "automatically.") from err
        if outcome == "ok":
            return True, "GPT accepted the request; AD provisioning is queued"
        if outcome == "rejected":
            return False, f"GPT rejected the request: {text}"
        raise OutcomeUnknown(
            f"GPT's reply for {userid} shows neither success nor a failure count: "
            f"{text!r}. Check GPT Pending Requests; this will not be retried "
            "automatically.")
