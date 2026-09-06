"""The API surface: webhook receiver, approval endpoints, run queries.

Run it with:

    uvicorn alm_api.main:app --host 0.0.0.0 --port 8080

Authentication is layered. Webhooks are HMAC-signed and replay-protected here;
human traffic is authenticated by Identity-Aware Proxy before it reaches this
process, and the approval endpoints additionally require a signed token bound to
one approval batch.
"""
from .security import issue_approval_token, verify_approval_token, verify_webhook

__all__ = ["issue_approval_token", "verify_approval_token", "verify_webhook"]
