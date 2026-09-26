"""Tests for alm_api.security: webhook HMAC verification, approval tokens, and replay guard."""
from __future__ import annotations

import time

from alm_api.security import (
    ReplayGuard,
    caller_identity,
    issue_approval_token,
    sign_payload,
    verify_approval_token,
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


def test_replay_guard_prevents_redelivery():
    guard = ReplayGuard(capacity=10)
    assert guard.check_and_add("msg-1") is True
    assert guard.check_and_add("msg-2") is True
    assert guard.check_and_add("msg-1") is False  # replay detected

    secret = "webhook-secret"  # pragma: allowlist secret
    body = b"payload"
    ts = str(time.time())
    sig = sign_payload(secret, f"{ts}.".encode() + body)

    ok1, _ = verify_webhook(
        secret, body=body, signature=sig, timestamp=ts, delivery_id="deliv-1", guard=guard
    )
    assert ok1 is True

    ok2, reason2 = verify_webhook(
        secret, body=body, signature=sig, timestamp=ts, delivery_id="deliv-1", guard=guard
    )
    assert ok2 is False
    assert "already been processed" in reason2


def test_approval_token_issue_and_verify_valid():
    secret = "approval-secret"  # pragma: allowlist secret
    thread_id = "thread-abc"
    plan_hash = "phash123456"
    expires = time.time() + 600

    token = issue_approval_token(
        secret, thread_id=thread_id, plan_hash=plan_hash, expires_at=expires
    )
    ok, reason, claims = verify_approval_token(
        secret, token, thread_id=thread_id, plan_hash=plan_hash
    )
    assert ok is True
    assert reason == "ok"
    assert claims["tid"] == thread_id
    assert claims["ph"] == plan_hash


def test_approval_token_tampered_signature_rejected():
    secret = "approval-secret"  # pragma: allowlist secret
    token = issue_approval_token(
        secret, thread_id="t1", plan_hash="p1", expires_at=time.time() + 600
    )
    payload, _ = token.split(".", 1)
    tampered = f"{payload}.badsig12345"

    ok, reason, _ = verify_approval_token(secret, tampered)
    assert ok is False
    assert reason == "signature mismatch"


def test_approval_token_expired_rejected():
    secret = "approval-secret"  # pragma: allowlist secret
    past = time.time() - 30
    token = issue_approval_token(
        secret, thread_id="t1", plan_hash="p1", expires_at=past
    )

    ok, reason, _ = verify_approval_token(secret, token)
    assert ok is False
    assert reason == "token expired"


def test_approval_token_plan_hash_mismatch_rejected():
    secret = "approval-secret"  # pragma: allowlist secret
    token = issue_approval_token(
        secret, thread_id="t1", plan_hash="original-hash", expires_at=time.time() + 600
    )

    ok, reason, _ = verify_approval_token(
        secret, token, thread_id="t1", plan_hash="changed-hash"
    )
    assert ok is False
    assert "the plan changed" in reason


def test_approval_token_missing_secret_fails_closed():
    ok, reason, _ = verify_approval_token("", "some.token")
    assert ok is False
    assert "no approval signing key is configured" in reason


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
