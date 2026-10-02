"""Telling approvers a run is waiting for them.

Built once per process and handed to the services, so a run parked by any
worker - inside the API or standalone - sends its card the same way.
"""
from __future__ import annotations

from alm_core.logging import get_logger

from .chat import build_card, post_card
from .security import issue_approval_token

log = get_logger("alm.notify")


def make_notifier(settings, resolver):
    """An async ``notifier(request)`` for the approval gate, or None when no
    channel is configured (the console still shows parked runs)."""
    if not getattr(settings, "chat_webhook_url", ""):
        return None

    def secret(name: str) -> str:
        try:
            return resolver.get(name)
        except Exception as err:  # noqa: BLE001 - a missing secret skips the card
            log.warning("secret_unavailable", secret=name, error=str(err))
            return ""

    async def notify(request) -> None:
        signing = secret(settings.approval_signing_secret_name)
        if not signing:
            log.warning("approval_token_unavailable", thread_id=request.thread_id)
            return
        token = issue_approval_token(signing, thread_id=request.thread_id,
                                     plan_hash=request.plan_hash,
                                     expires_at=request.expires_at.timestamp())
        base = settings.approval_base_url.rstrip("/")
        card = build_card(
            request,
            approve_url=f"{base}/approvals/{request.thread_id}?decision=approve&token={token}",
            reject_url=f"{base}/approvals/{request.thread_id}?decision=reject&token={token}",
            review_url=f"{base}/approvals/{request.thread_id}?token={token}")
        post_card(settings.chat_webhook_url, card)

    return notify
