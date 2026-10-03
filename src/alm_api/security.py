"""Webhook authentication, and who an identity-aware proxy says the caller is.

Two independent problems:

**Inbound webhooks.** EWM has no first-class outbound webhook, so whatever
bridges to us (a follow-up action plugin, or an intermediary) posts over the
network into a service that can write to production. Requests are authenticated
with an HMAC over the raw body, bound to a timestamp so a captured request
cannot be replayed later. The delivery id is recorded in the store
(``remember_delivery``), so a replay inside the window is refused by every
replica, not only the one that saw it first.

**Caller identity behind a proxy.** With ``ALM_AUTH_MODE=iap`` the proxy signs
people in; :func:`caller_identity` reads - and with ``ALM_IAP_AUDIENCE``,
verifies - what it asserts. (With ``oidc`` the service signs people in
itself: see ``alm_api.auth``.) Approval cards carry no capability at all: they
link to the console, where a signed-in approver decides.

Every comparison here is constant-time. A timing oracle on an HMAC is a real
attack, not a theoretical one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from alm_core.logging import get_logger

log = get_logger("alm.api.security")

# How far a webhook timestamp may be from now. Generous enough for clock skew,
# tight enough that a captured request is useless tomorrow.
WEBHOOK_TOLERANCE_SECONDS = 300


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_payload(secret: str, payload: bytes) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def verify_webhook(secret: str, *, body: bytes, signature: str,
                   timestamp: str) -> tuple[bool, str]:
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

    # Replays inside the window are caught by the caller against the store
    # (remember_delivery), so every replica sees every delivery id.
    return True, "ok"


# --------------------------------------------------------------- identity

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


def _verify_iap_jwt(assertion: str, audience: str) -> dict:
    """Check the IAP-signed JWT: signature (IAP's public keys), audience, expiry."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return dict(id_token.verify_token(assertion, google_requests.Request(),
                                      audience=audience, certs_url=IAP_CERTS_URL))


def caller_identity(headers, *, iap_audience: str | None = None,
                    verifier=_verify_iap_jwt) -> str:
    """The approver's identity, as established by the Identity-Aware Proxy.

    With ``ALM_IAP_AUDIENCE`` set (``/projects/<number>/global/backendServices/
    <id>``), only the signed ``x-goog-iap-jwt-assertion`` counts: its signature,
    audience and expiry are verified and its ``email`` claim is the identity. A
    request that reached the service without passing through IAP cannot forge
    that, whereas it can set any plain header it likes.

    Without an audience (a deployment that has not configured it yet) the
    plain IAP headers are read, which is only as safe as the ingress rule that
    keeps the service behind IAP. ``x-forwarded-user`` is never trusted: it is
    not an IAP header, and a caller can always send it.

    Anything unverifiable is ``unknown`` rather than a name - an audit row
    naming the wrong person is worse than one admitting it does not know, and
    the approval endpoint refuses an unknown caller without a signed token.
    """
    audience = (os.getenv("ALM_IAP_AUDIENCE", "") if iap_audience is None
                else iap_audience).strip()
    if audience:
        assertion = headers.get("x-goog-iap-jwt-assertion", "")
        if not assertion:
            return "unknown"
        try:
            claims = verifier(assertion, audience)
        except Exception as err:  # noqa: BLE001 - any failure means "not verified"
            log.warning("iap_jwt_rejected", error=type(err).__name__)
            return "unknown"
        return str(claims.get("email") or "unknown")

    for header in ("x-goog-authenticated-user-email", "x-goog-authenticated-user-id"):
        value = headers.get(header)
        if value:
            return value.split(":", 1)[-1] if ":" in value else value
    return "unknown"
