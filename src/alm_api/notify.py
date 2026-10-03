"""Telling approvers a run is waiting for them.

Built once per process and handed to the services, so a run parked by any
worker - inside the API or standalone - announces itself the same way, on
every channel in ``ALM_NOTIFY_CHANNELS``:

* ``chat``  - a Google Chat card (``ALM_CHAT_WEBHOOK_URL``)
* ``teams`` - a Microsoft Teams message (``ALM_TEAMS_WEBHOOK_URL``)
* ``email`` - an e-mail to ``ALM_APPROVER_EMAILS`` over SMTP with STARTTLS

None of them can approve anything: each carries a link to the run in the
console, where a signed-in approver decides. A channel that fails is logged
and skipped; the run stays parked and visible in the console regardless.
"""
from __future__ import annotations

import html
import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from alm_core.logging import get_logger

from .chat import build_card, post_card

log = get_logger("alm.notify")


def console_url(settings, thread_id: str) -> str:
    base = (getattr(settings, "approval_base_url", "") or "").rstrip("/")
    return f"{base}/?run={urllib.parse.quote(thread_id)}"


def _channels(settings) -> list[str]:
    return [c.strip().lower() for c in (getattr(settings, "notify_channels", "") or "").split(",")
            if c.strip()]


def summary(request, needed: int) -> str:
    high = [i.userid for i in request.items
            if str(getattr(i.risk, "value", i.risk)).lower() == "high"]
    lines = [f"{request.user_count} user(s) on {request.work_item_count} work item(s), "
             f"{request.environment}. {needed} approver(s) needed.",
             f"Expires {request.expires_at:%Y-%m-%d %H:%M} UTC."]
    if high:
        lines.append(f"Needs a closer look: {', '.join(high)}.")
    return "\n".join(lines)


def post_teams(webhook_url: str, request, *, url: str, needed: int,
               timeout: float = 15.0) -> bool:
    """A Teams message with one button to the console (incoming webhook)."""
    if urllib.parse.urlsplit(webhook_url).scheme.lower() != "https":
        log.warning("teams_insecure_scheme")
        return False
    card = {
        "@type": "MessageCard", "@context": "https://schema.org/extensions",
        "summary": "ALM provisioning approval",
        "themeColor": "B3261E" if request.environment.upper() == "PROD" else "0B6E69",
        "title": ("ALM provisioning - PRODUCTION approval"
                  if request.environment.upper() == "PROD" else "ALM provisioning - approval"),
        "text": html.escape(summary(request, needed)).replace("\n", "<br>"),
        "potentialAction": [{"@type": "OpenUri", "name": "Review and decide in the console",
                             "targets": [{"os": "default", "uri": url}]}],
    }
    message = urllib.request.Request(  # noqa: S310 - https checked above
        webhook_url, data=json.dumps(card).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(message, timeout=timeout) as response:  # noqa: S310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected - https only, checked above
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError) as err:
        log.warning("teams_failed", error=str(err))
        return False


def send_email(settings, request, *, url: str, needed: int, password: str = "") -> bool:
    recipients = [a.strip() for a in settings.approver_emails.split(",") if a.strip()]
    if not (settings.smtp_host and settings.smtp_from and recipients):
        log.info("email_not_configured")
        return False
    message = EmailMessage()
    message["Subject"] = (f"[ALM {request.environment}] Approval needed: "
                          f"{request.user_count} user(s)")
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(f"{summary(request, needed)}\n\nReview and decide in the console:\n"
                        f"{url}\n\nThis e-mail cannot approve anything.")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if password:
                smtp.login(settings.smtp_from, password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as err:
        log.warning("email_failed", error=str(err))
        return False


def make_notifier(settings, resolver):
    """An async ``notifier(request)`` for the approval gate, or None when no
    channel is configured (the console still lists parked runs)."""
    channels = _channels(settings)
    if not channels:
        return None

    def secret(name: str) -> str:
        try:
            return resolver.get(name)
        except Exception:  # noqa: BLE001 - optional secrets
            return ""

    async def notify(request) -> None:
        from alm_agents.approval_policy import approvers_needed

        needed = approvers_needed(request, settings)
        url = console_url(settings, request.thread_id)
        sent = {}
        if "chat" in channels and settings.chat_webhook_url:
            sent["chat"] = post_card(settings.chat_webhook_url,
                                     build_card(request, review_url=url, needed=needed))
        if "teams" in channels and settings.teams_webhook_url:
            sent["teams"] = post_teams(settings.teams_webhook_url, request, url=url,
                                       needed=needed)
        if "email" in channels:
            sent["email"] = send_email(settings, request, url=url, needed=needed,
                                       password=secret(settings.smtp_password_secret_name))
        log.info("approval_announced", thread_id=request.thread_id, sent=sent)

    return notify
