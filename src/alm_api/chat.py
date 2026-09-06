"""The Google Chat card an approver actually reads.

Design rule: the card must make the *risky* thing the obvious thing to look at.
An approver scanning seventeen identical green rows will approve the eighteenth
without reading it, so high-risk users are listed first, their reasons are shown
inline, and the card states plainly when it is asking about production.

The card's buttons carry a signed, expiring token bound to this thread and this
plan hash. It is not a bearer capability to approve anything else, and it stops
working the moment the plan changes. The link lands on the Cloud Run approval
endpoint behind Identity-Aware Proxy, so the approver is authenticated by Google
before the request reaches this service - which is what makes the audit row's
approver field trustworthy.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from alm_core.logging import get_logger
from alm_core.models import ApprovalRequest, RiskLevel

log = get_logger("alm.api.chat")

# Google Chat's decoratedText icons; colour comes from the text itself, since
# cards v2 has no per-widget colour.
_RISK_MARK = {RiskLevel.HIGH: "&#9888;", RiskLevel.MEDIUM: "&#9679;", RiskLevel.LOW: "&#9675;"}


def _user_widget(item) -> dict:
    lines = [
        f"<b>{item.action}</b>",
        f"Work items: {', '.join(item.work_item_ids) or '-'}",
        f"Registry state: {item.state.value}",
    ]
    if item.risk_reasons:
        lines.append("<br>".join(f"&bull; {r}" for r in item.risk_reasons))
    top = f"{_RISK_MARK.get(item.risk, '')} <b>{item.userid}</b>"
    if item.display_name:
        top += f" &mdash; {item.display_name}"
    if item.risk == RiskLevel.HIGH:
        top = f"<font color=\"#B3261E\">{top}</font>"
    return {
        "decoratedText": {
            "topLabel": item.risk.value.upper(),
            "text": top,
            "bottomLabel": " | ".join(lines[:2]),
            "wrapText": True,
        }
    }


def build_card(request: ApprovalRequest, *, approve_url: str, reject_url: str,
               review_url: str = "") -> dict:
    """A Google Chat cards v2 message for an incoming webhook."""
    is_prod = request.environment.upper() == "PROD"
    high = [i for i in request.items if i.risk == RiskLevel.HIGH]

    header_sections: list[dict] = [{
        "widgets": [{
            "textParagraph": {
                "text": (f"<b>{request.user_count}</b> user(s) across "
                         f"<b>{request.work_item_count}</b> work item(s) &middot; "
                         f"environment <b>{request.environment}</b><br>"
                         f"Expires {request.expires_at:%Y-%m-%d %H:%M} UTC")
            }
        }]
    }]

    if high:
        header_sections.append({"widgets": [{
            "textParagraph": {
                "text": (f"<font color=\"#B3261E\"><b>{len(high)} user(s) need a closer "
                         f"look:</b> {', '.join(i.userid for i in high)}</font>")
            }
        }]})

    detail = {
        "header": "Users in this batch",
        "collapsible": True,
        "uncollapsibleWidgetsCount": min(3, len(request.items)),
        "widgets": [_user_widget(item) for item in request.items],
    }

    buttons = [
        {"text": "Approve all", "onClick": {"openLink": {"url": approve_url}},
         "color": {"red": 0.05, "green": 0.43, "blue": 0.42, "alpha": 1}},
        {"text": "Reject", "onClick": {"openLink": {"url": reject_url}}},
    ]
    if review_url:
        buttons.append({"text": "Review in browser",
                        "onClick": {"openLink": {"url": review_url}}})

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

    A card that does not send must not lose the run: the approval is still
    reachable at its URL, and the run stays parked at the interrupt.
    """
    if not webhook_url:
        log.info("chat_not_configured", reason="no ALM_CHAT_WEBHOOK_URL")
        return False
    request = urllib.request.Request(
        webhook_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=UTF-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ok = 200 <= response.status < 300
            if not ok:
                log.warning("chat_card_rejected", status=response.status)
            return ok
    except (urllib.error.URLError, TimeoutError) as err:
        log.warning("chat_card_failed", error=str(err))
        return False
