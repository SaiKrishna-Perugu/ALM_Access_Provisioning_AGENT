"""Approval announcements: every channel links to the console, none can approve."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")

from alm_api import notify  # noqa: E402
from alm_core.models import ApprovalItem, ApprovalRequest, RiskLevel, utcnow  # noqa: E402


def request(environment="PROD"):
    return ApprovalRequest(
        run_id="r1", thread_id="wi-1001", environment=environment,
        expires_at=utcnow() + timedelta(hours=1), plan_hash="p",
        items=[ApprovalItem(userid="AB12345", work_item_ids=["1001"], risk=RiskLevel.HIGH)])


def settings(**extra):
    base = {"notify_channels": "chat,teams,email", "approval_base_url": "https://alm.example.com",
            "chat_webhook_url": "https://chat.example.com/hook",
            "teams_webhook_url": "https://teams.example.com/hook", "smtp_host": "smtp.example.com",
            "smtp_port": 587, "smtp_from": "alm@example.com",
            "approver_emails": "a@example.com, b@example.com",
            "smtp_password_secret_name": "smtp", "environment": "PROD",  # pragma: allowlist secret
            "approvers_required": 1, "approvers_required_prod": 2,
            "approvers_required_high_risk": 2}
    base.update(extra)
    return SimpleNamespace(**base)


def test_every_channel_carries_the_console_link_and_no_approval(monkeypatch):
    posted, mails = [], []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def urlopen(req, timeout=None):
        posted.append((req.full_url, json.loads(req.data)))
        return Response()

    class Smtp:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def starttls(self, context=None):
            mails.append("tls")

        def login(self, user, password):
            mails.append(("login", user))

        def send_message(self, message):
            mails.append(message)

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("alm_api.chat.urllib.request.urlopen", urlopen)
    monkeypatch.setattr(notify.smtplib, "SMTP", Smtp)
    notifier = notify.make_notifier(settings(), SimpleNamespace(get=lambda _n: "pw"))
    asyncio.run(notifier(request()))

    link = "https://alm.example.com/?run=wi-1001"
    urls = [u for u, _ in posted]
    assert urls == ["https://chat.example.com/hook", "https://teams.example.com/hook"]
    for _url, body in posted:
        text = json.dumps(body)
        assert link in text and "token" not in text and "decision=" not in text
    message = mails[-1]
    assert "tls" in mails and ("login", "alm@example.com") in mails
    assert link in message.get_content() and "cannot approve" in message.get_content()
    assert message["To"] == "a@example.com, b@example.com"
    assert "2 approver(s) needed" in message.get_content()


def test_no_channel_means_no_notifier_and_http_links_are_refused(monkeypatch):
    assert notify.make_notifier(settings(notify_channels=""), None) is None
    assert not notify.post_teams("http://teams.example.com/hook", request(),
                                 url="https://x", needed=1)


def test_text_a_requester_wrote_cannot_put_markup_on_a_card():
    """A display name or reason can come from the request; cards render HTML."""
    from alm_api.chat import build_card

    hostile = ApprovalRequest(
        run_id="r1", thread_id="wi-1001", environment="TEST<b>",
        expires_at=utcnow() + timedelta(hours=1), plan_hash="p",
        items=[ApprovalItem(userid="AB12345", work_item_ids=["1001"], risk=RiskLevel.HIGH,
                            display_name='<a href="https://evil.example">Approve here</a>',
                            risk_reasons=["<img src=x onerror=alert(1)>"])])
    card = json.dumps(build_card(hostile, review_url="https://alm.example.com/?run=wi-1001"))
    assert "<a href" not in card and "<img" not in card and "TEST<b>" not in card
    assert "&lt;a href=" in card and "&lt;img" in card

    posted = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def urlopen(req, timeout=None):
        posted.append(json.loads(req.data))
        return Response()

    import alm_api.notify as module

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = urlopen
    try:
        assert notify.post_teams("https://teams.example.com/hook", hostile,
                                 url="https://alm.example.com/?run=wi-1001", needed=1)
    finally:
        module.urllib.request.urlopen = original
    assert "TEST<b>" not in posted[0]["text"] and "TEST&lt;b&gt;" in posted[0]["text"]
