"""Security and validation tests for alm_api.chat: scheme validation and card building."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# alm_core needs pydantic: these run in the agents CI job, and skip in the
# CLI-only test job that installs requirements.txt alone.
pytest.importorskip("pydantic")

from alm_api.chat import build_card, post_card  # noqa: E402
from alm_core.models import (  # noqa: E402
    ApprovalItem,
    ApprovalRequest,
    RiskLevel,
    UserState,
)


def test_post_card_refuses_non_https_urls():
    # file:// scheme refused
    assert post_card("file:///etc/passwd", {"test": "data"}) is False

    # http:// scheme refused
    assert post_card("http://insecure.example.com/webhook", {"test": "data"}) is False

    # ftp:// scheme refused
    assert post_card("ftp://ftp.example.com/upload", {"test": "data"}) is False

    # empty url
    assert post_card("", {"test": "data"}) is False


def test_post_card_allows_https_url():
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = post_card("https://chat.googleapis.com/v1/spaces/XYZ/messages", {"cardsV2": []})
        assert ok is True
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.startswith("https://")


def test_build_card_highlights_high_risk_users():
    items = [
        ApprovalItem(
            userid="AB12345",
            action="import_user",
            risk=RiskLevel.HIGH,
            risk_reasons=["previously disabled", "cross-domain request"],
            state=UserState.READY,
            work_item_ids=["WI-101"],
            display_name="User One",
        ),
        ApprovalItem(
            userid="CD67890",
            action="import_user",
            risk=RiskLevel.LOW,
            risk_reasons=[],
            state=UserState.READY,
            work_item_ids=["WI-102"],
            display_name="User Two",
        ),
    ]
    request = ApprovalRequest(
        run_id="run-001",
        thread_id="th-001",
        environment="PROD",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        items=items,
        plan_hash="hashabc123",
    )

    card = build_card(
        request,
        approve_url="https://alm.example.com/approve?tok=1",
        reject_url="https://alm.example.com/reject?tok=1",
    )

    card_str = str(card)
    assert "PRODUCTION" in card_str
    assert "AB12345" in card_str
    assert "need a closer look" in card_str
