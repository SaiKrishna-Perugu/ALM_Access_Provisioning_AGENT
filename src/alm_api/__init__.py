"""The API surface: webhook receiver, approval endpoints, run queries.

Run it with:

    uvicorn alm_api.main:app --host 0.0.0.0 --port 8080

Authentication is layered. Webhooks are HMAC-signed and replay-protected;
people are signed in by an identity-aware proxy (``ALM_AUTH_MODE=iap``) or by
the service itself against the company IdP (``oidc``), and every endpoint
checks their role (``alm_api.auth``).
"""
from .security import caller_identity, verify_webhook

__all__ = ["caller_identity", "verify_webhook"]
