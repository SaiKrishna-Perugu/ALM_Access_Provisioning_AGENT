"""The Google Chat card an approver actually reads.

Design rule: the card must make the *risky* thing the obvious thing to look at.
An approver scanning seventeen identical green rows will approve the eighteenth
without reading it, so high-risk users are listed first, their reasons are shown
inline, and the card states plainly when it is asking about production.

The card cannot approve anything. Its one button opens the run in the
console, where the approver is signed in (OIDC or IAP), holds the approver role,
ticks the users they approve, and decides. A link in a chat message is not an
identity; the console is - which is what makes the audit row's approver field
trustworthy.
"""
from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request

from alm_core.logging import get_logger
from alm_core.models import ApprovalRequest, RiskLevel

log = get_logger("alm.api.chat")

# Google Chat's decoratedText icons; colour comes from the text itself, since
# cards v2 has no per-widget colour.
_RISK_MARK = {RiskLevel.HIGH: "&#9888;", RiskLevel.MEDIUM: "&#9679;", RiskLevel.LOW: "&#9675;"}


def _e(value) -> str:
    """Text for a card's HTML. Names and reasons can come from what a requester
    typed, and Chat renders the markup an approver sees."""
    return html.escape(str(value), quote=True)


def _user_widget(item) -> dict:
    top = f"{_RISK_MARK.get(item.risk, '')} <b>{_e(item.userid)}</b>"
    if item.display_name:
        top += f" &mdash; {_e(item.display_name)}"
    if item.risk == RiskLevel.HIGH:
        top = f"<font color=\"#B3261E\">{top}</font>"
    # text takes Chat's HTML subset; the labels are plain text.
    text = [top]
    if item.action:
        text.append(_e(item.action))
    text += [f"&bull; {_e(r)}" for r in item.risk_reasons]
    return {
        "decoratedText": {
            "topLabel": item.risk.value.upper(),
            "text": "<br>".join(text),
            "bottomLabel": (f"Work items: {', '.join(item.work_item_ids) or '-'} | "
                            f"Registry state: {item.state.value}"),
            "wrapText": True,
        }
    }


def build_card(request: ApprovalRequest, *, review_url: str, needed: int = 1) -> dict:
    """A Google Chat cards v2 message for an incoming webhook."""
    is_prod = request.environment.upper() == "PROD"
    high = [i for i in request.items if i.risk == RiskLevel.HIGH]

    header_sections: list[dict] = [{
        "widgets": [{
            "textParagraph": {
                "text": (f"<b>{int(request.user_count)}</b> user(s) across "  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format - every value escaped or an int
                         f"<b>{int(request.work_item_count)}</b> work item(s) &middot; "  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format
                         f"environment <b>{_e(request.environment)}</b> &middot; "  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format
                         f"<b>{int(needed)}</b> approver(s) needed<br>"
                         f"Expires {request.expires_at:%Y-%m-%d %H:%M} UTC")
            }
        }]
    }]

    if high:
        header_sections.append({"widgets": [{
            "textParagraph": {
                "text": (f"<font color=\"#B3261E\"><b>{len(high)} user(s) need a closer "  # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format - escaped
                         f"look:</b> {_e(', '.join(i.userid for i in high))}</font>")
            }
        }]})

    detail = {
        "header": "Users in this batch",
        "collapsible": True,
        "uncollapsibleWidgetsCount": min(3, len(request.items)),
        "widgets": [_user_widget(item) for item in request.items],
    }

    buttons = [
        {"text": "Review and decide in the console",
         "onClick": {"openLink": {"url": review_url}},
         "color": {"red": 0.05, "green": 0.43, "blue": 0.42, "alpha": 1}},
    ]

    return {
        "cardsV2": [{
            "cardId": f"alm-approval-{request.thread_id}",
            "card": {
                "header": {
                    "title": ("ALM provisioning &mdash; PRODUCTION approval"
                              if is_prod else "ALM provisioning &mdash; approval"),
                    "subtitle": f"run {request.run_id} &middot; plan {request.plan_hash[:12]}",
                    "imageType": "CIRCLE",
                },
                "sections": header_sections + [detail, {"widgets": [{"buttonList": {
                    "buttons": buttons}}]}],
            },
        }]
    }


def post_card(webhook_url: str, payload: dict, *, timeout: float = 15.0) -> bool:
    """Deliver the card. Failure is logged, never fatal.

    A card that does not send must not lose the run: it stays parked at the
    interrupt and listed in the console as awaiting approval.
    """
    if not webhook_url:
        log.info("chat_not_configured", reason="no ALM_CHAT_WEBHOOK_URL")
        return False
    parsed = urllib.parse.urlsplit(webhook_url)
    if parsed.scheme.lower() != "https":
        log.warning("chat_card_insecure_scheme", scheme=parsed.scheme)
        return False
    request = urllib.request.Request(  # noqa: S310 - URL scheme verified to be https only
        webhook_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=UTF-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected - https only, checked above
            ok = 200 <= response.status < 300
            if not ok:
                log.warning("chat_card_rejected", status=response.status)
            return ok
    except (urllib.error.URLError, TimeoutError) as err:
        log.warning("chat_card_failed", error=str(err))
        return False
