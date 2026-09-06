"""Webhook authentication and signed approval tokens.

Two independent problems:

**Inbound webhooks.** EWM has no first-class outbound webhook, so whatever
bridges to us (a follow-up action plugin, or an intermediary) posts over the
network into a service that can write to production. Requests are authenticated
with an HMAC over the raw body, bound to a timestamp so a captured request
cannot be replayed later, and to a delivery id so it cannot be replayed twice
inside the window.

**Outbound approval links.** A Teams card carries a URL a human clicks. That URL
must not be a bearer capability to approve anything: the token is bound to one
thread and one plan hash, expires, and carries no privilege of its own - the API
still records who the caller was.

Every comparison here is constant-time. A timing oracle on an HMAC is a real
attack, not a theoretical one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import OrderedDict

from alm_core.logging import get_logger

log = get_logger("alm.api.security")

# How far a webhook timestamp may be from now. Generous enough for clock skew,
# tight enough that a captured request is useless tomorrow.
WEBHOOK_TOLERANCE_SECONDS = 300
# Bounded memory for replay detection; the timestamp window does the rest.
SEEN_DELIVERY_CAPACITY = 4096


class ReplayGuard:
    """Remembers recent delivery ids so a redelivery inside the window is caught."""

    def __init__(self, capacity: int = SEEN_DELIVERY_CAPACITY):
        self.capacity = capacity
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check_and_add(self, delivery_id: str) -> bool:
        """True if this is the first time we have seen the id."""
        if not delivery_id:
            return True  # nothing to key on; the timestamp window still applies
        now = time.time()
        cutoff = now - WEBHOOK_TOLERANCE_SECONDS * 2
        while self._seen and next(iter(self._seen.values())) < cutoff:
            self._seen.popitem(last=False)
        if delivery_id in self._seen:
            return False
        self._seen[delivery_id] = now
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_payload(secret: str, payload: bytes) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def verify_webhook(secret: str, *, body: bytes, signature: str, timestamp: str,
                   delivery_id: str = "", guard: ReplayGuard | None = None
                   ) -> tuple[bool, str]:
    """Authenticate an inbound webhook. Returns ``(ok, reason)``.

    The signature covers ``timestamp.body`` so a valid signature cannot be
    lifted onto a different timestamp.
    """
    if not secret:
        return False, "no webhook secret is configured"
    if not signature:
        return False, "missing signature header"
    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError):
        return False, "missing or malformed timestamp header"

    drift = abs(time.time() - sent_at)
    if drift > WEBHOOK_TOLERANCE_SECONDS:
        return False, f"timestamp is {int(drift)}s away from now"

    expected = sign_payload(secret, f"{timestamp}.".encode() + body)
    provided = signature.split("=", 1)[-1]  # tolerate a "sha256=" prefix
    if not hmac.compare_digest(expected, provided):
        return False, "signature mismatch"

    if guard is not None and not guard.check_and_add(delivery_id):
        return False, f"delivery {delivery_id} has already been processed"
    return True, "ok"


# ------------------------------------------------------------ approval tokens

def issue_approval_token(secret: str, *, thread_id: str, plan_hash: str,
                         expires_at: float, audience: str = "approval") -> str:
    """A short-lived token bound to one approval batch."""
    claims = {"tid": thread_id, "ph": plan_hash, "exp": int(expires_at), "aud": audience}
    payload = _b64e(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{sign_payload(secret, payload.encode('ascii'))}"


def verify_approval_token(secret: str, token: str, *, thread_id: str = "",
                          plan_hash: str = "", audience: str = "approval"
                          ) -> tuple[bool, str, dict]:
    """Validate a token. Returns ``(ok, reason, claims)``."""
    if not secret:
        return False, "no approval signing key is configured", {}
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return False, "malformed token", {}

    if not hmac.compare_digest(sign_payload(secret, payload.encode("ascii")), signature):
        return False, "signature mismatch", {}
    try:
        claims = json.loads(_b64d(payload))
    except (ValueError, TypeError):
        return False, "unreadable claims", {}

    if claims.get("aud") != audience:
        return False, "wrong audience", claims
    if float(claims.get("exp", 0)) < time.time():
        return False, "token expired", claims
    if thread_id and claims.get("tid") != thread_id:
        return False, "token is for a different approval", claims
    # Binding to the plan hash is what stops a token approving a batch that
    # changed after the card was sent.
    if plan_hash and claims.get("ph") != plan_hash:
        return False, "the plan changed after this token was issued", claims
    return True, "ok", claims


def caller_identity(headers) -> str:
    """Best-effort approver identity from the Identity-Aware Proxy headers.

    IAP validates the Google sign-in and injects these before the request
    reaches Cloud Run, so this service does not verify a JWT itself. The header
    value is prefixed (``accounts.google.com:alice@example.com``); the prefix is
    stripped for readability but the address is recorded verbatim.

    When no header is present the approver is recorded as ``unknown`` rather
    than being invented - an audit row naming the wrong person is worse than one
    admitting it does not know.

    Note the deployment assumption: Cloud Run ingress must be internal +
    load-balancer only, with IAP in front. A service reachable directly would
    let a caller present these headers themselves.
    """
    for header in ("x-goog-authenticated-user-email",
                   "x-goog-authenticated-user-id",
                   "x-forwarded-user"):
        value = headers.get(header)
        if value:
            return value.split(":", 1)[-1] if ":" in value else value
    return "unknown"
