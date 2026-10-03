"""Tests for alm_api.security: webhook HMAC verification and caller identity."""
from __future__ import annotations

import time

import pytest

# alm_api needs alm_core, which needs pydantic: skipped in the CLI-only job.
pytest.importorskip("pydantic")

from alm_api.security import (  # noqa: E402
    caller_identity,
    sign_payload,
    verify_webhook,
)


def test_sign_payload_deterministic():
    secret = "secret-key-12345"  # pragma: allowlist secret
    payload = b"test payload"
    sig1 = sign_payload(secret, payload)
    sig2 = sign_payload(secret, payload)
    assert sig1 == sig2
    assert isinstance(sig1, str)
    assert len(sig1) > 0


def test_verify_webhook_valid():
    secret = "webhook-secret-xyz"  # pragma: allowlist secret
    body = b'{"event": "work_item_updated", "id": 12345}'
    ts = str(time.time())
    sig = sign_payload(secret, f"{ts}.".encode() + body)

    ok, reason = verify_webhook(secret, body=body, signature=sig, timestamp=ts)
    assert ok is True
    assert reason == "ok"


def test_verify_webhook_tampered_signature_rejected():
    secret = "webhook-secret-xyz"  # pragma: allowlist secret
    body = b'{"event": "work_item_updated"}'
    ts = str(time.time())

    ok, reason = verify_webhook(
        secret, body=body, signature="invalidsignature123", timestamp=ts
    )
    assert ok is False
    assert reason == "signature mismatch"


def test_verify_webhook_missing_secret_fails_closed():
    ok, reason = verify_webhook(
        "", body=b"data", signature="sig", timestamp=str(time.time())
    )
    assert ok is False
    assert "no webhook secret is configured" in reason


def test_verify_webhook_missing_or_malformed_timestamp():
    secret = "webhook-secret"  # pragma: allowlist secret
    body = b"data"
    sig = sign_payload(secret, f"bad.{body.decode()}".encode())

    ok, reason = verify_webhook(secret, body=body, signature=sig, timestamp="not-a-number")
    assert ok is False
    assert "timestamp" in reason


def test_verify_webhook_expired_timestamp_rejected():
    secret = "webhook-secret"  # pragma: allowlist secret
    body = b"data"
    old_ts = str(time.time() - 400)
    sig = sign_payload(secret, f"{old_ts}.".encode() + body)

    ok, reason = verify_webhook(secret, body=body, signature=sig, timestamp=old_ts)
    assert ok is False
    assert "away from now" in reason


def test_caller_identity_iap_headers(monkeypatch):
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    headers = {"x-goog-authenticated-user-email": "accounts.google.com:alice@example.com"}
    assert caller_identity(headers) == "alice@example.com"

    headers_id = {"x-goog-authenticated-user-id": "accounts.google.com:123456"}
    assert caller_identity(headers_id) == "123456"

    assert caller_identity({}) == "unknown"


def test_x_forwarded_user_is_never_trusted(monkeypatch):
    """Not an IAP header: any caller can send it, and it used to name the approver."""
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    assert caller_identity({"x-forwarded-user": "bob@example.com"}) == "unknown"


AUDIENCE = "/projects/123/global/backendServices/456"


def test_with_an_audience_only_a_verified_iap_jwt_counts():
    seen = []

    def verifier(assertion, audience):
        seen.append((assertion, audience))
        return {"email": "alice@example.com"}

    headers = {"x-goog-iap-jwt-assertion": "signed.jwt.value",
               # Plain headers are ignored once an audience is configured.
               "x-goog-authenticated-user-email": "accounts.google.com:mallory@example.com"}
    assert caller_identity(headers, iap_audience=AUDIENCE,
                           verifier=verifier) == "alice@example.com"
    assert seen == [("signed.jwt.value", AUDIENCE)]


def test_a_forged_or_missing_iap_jwt_is_unknown():
    def reject(_assertion, _audience):
        raise ValueError("Token has wrong audience")

    forged = {"x-goog-iap-jwt-assertion": "forged.jwt",
              "x-goog-authenticated-user-email": "accounts.google.com:mallory@example.com"}
    assert caller_identity(forged, iap_audience=AUDIENCE, verifier=reject) == "unknown"
    plain_only = {"x-goog-authenticated-user-email": "accounts.google.com:mallory@example.com"}
    assert caller_identity(plain_only, iap_audience=AUDIENCE, verifier=reject) == "unknown"


def test_the_audience_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ALM_IAP_AUDIENCE", AUDIENCE)
    headers = {"x-goog-authenticated-user-email": "accounts.google.com:mallory@example.com"}
    assert caller_identity(headers) == "unknown"  # no signed assertion, so no name
