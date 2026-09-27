"""Shared Jazz/HTTP plumbing: one session factory, one form-auth POST.

Before this module every script built its own ``requests.Session``, switched off
certificate verification, and re-implemented the Jazz ``j_security_check`` handshake.
A fix had to be applied in six places, which is how the TEST/PROD login
divergence went unnoticed for so long.

What is deliberately *not* centralised is the post-login **verification** step.
Each caller still proves the session works using a signal from its own service
(EWM: a protected OSLC resource returns XML; JTS: ``/whoami`` names a user).
That is the "verification must not share a failure mode with the action"
principle from the review: a single shared truthiness check is exactly what
produced the login-page-as-evidence incident.

Retries are GET-only by design. A retried POST against the Jazz comment factory
or ``multipleNewContributors`` would risk duplicating a write, so write retries
are opt-in per call site via :func:`post_with_retry`, which the caller may only
use once it has an idempotency check in front of it.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

import alm_config

try:  # urllib3 2.x
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

# (connect, read) seconds. Every request in the toolkit gets a timeout; an
# untimed request against a hung intranet host blocks the operator's terminal
# indefinitely.
DEFAULT_TIMEOUT = (15, 120)

_RETRY_STATUS = (429, 500, 502, 503, 504)


def _retry(total: int, methods) -> Retry:
    kwargs = {
        "total": total,
        "connect": total,
        "read": total,
        "status": total,
        "backoff_factor": 1.5,       # 0s, 1.5s, 3s, 6s ...
        "status_forcelist": _RETRY_STATUS,
        "raise_on_status": False,
        "respect_retry_after_header": True,
    }
    try:
        return Retry(allowed_methods=methods, **kwargs)
    except TypeError:  # urllib3 < 1.26 spelled it method_whitelist
        return Retry(method_whitelist=methods, **kwargs)


def harden(session: requests.Session, retries: int = 3) -> requests.Session:
    """Apply the TLS policy and GET retry adapter to an existing session.

    Separate from :func:`make_session` so the login helpers can also fix up a
    session a caller constructed itself, rather than silently running one branch
    of the toolkit unverified.
    """
    session.verify = alm_config.tls_verify()
    adapter = HTTPAdapter(max_retries=_retry(retries, frozenset(["GET", "HEAD", "OPTIONS"])))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def make_session(retries: int = 3) -> requests.Session:
    """A session with the configured TLS policy and bounded GET retries."""
    return harden(requests.Session(), retries)


def login_endpoints(session: requests.Session, server: str) -> list[str]:
    """Candidate ``j_security_check`` URLs for this server, best guess first.

    PROD posts to ``/authenticated/j_security_check`` while TEST posts to
    ``/auth/j_security_check``; the correct one is discovered by following the
    auth challenge, with the classic path kept as a fallback.
    """
    try:
        probe = session.get(f"{server}/authenticated/identity", allow_redirects=True,
                            timeout=DEFAULT_TIMEOUT)
        parts = urlsplit(probe.url)
        discovered = urlunsplit((parts.scheme, parts.netloc,
                                 parts.path.rsplit("/", 1)[0] + "/j_security_check", "", ""))
    except requests.RequestException:
        discovered = ""
    classic = f"{server}/authenticated/j_security_check"
    return [u for u in dict.fromkeys([discovered, classic]) if u]


def post_credentials(session: requests.Session, login_url: str, user: str, password: str):
    """POST the Jazz form-auth credentials. Returns the response.

    Never logs, prints or returns the password.
    """
    return session.post(login_url, data={"j_username": user, "j_password": password},
                        allow_redirects=True, timeout=DEFAULT_TIMEOUT)


def auth_failed(response) -> bool:
    """True when Jazz redirected to its authfailed page (bad credentials)."""
    seen = [response.url] + [h.headers.get("Location", "") for h in response.history]
    return any("authfailed" in (url or "") for url in seen)


def form_login(session: requests.Session, server: str, user: str, password: str,
               verify_session) -> bool:
    """Run Jazz form auth against every candidate endpoint.

    ``verify_session`` is a callable taking the session and returning True only
    when a *service-specific* protected resource proves the session is live. It
    must not simply re-test what this function already did.
    """
    for login_url in login_endpoints(session, server):
        try:
            response = post_credentials(session, login_url, user, password)
        except requests.RequestException:
            continue
        if auth_failed(response):
            continue
        try:
            if verify_session(session):
                return True
        except requests.RequestException:
            continue
    return False


def post_with_retry(session: requests.Session, url: str, *, attempts: int = 3,
                    backoff: float = 2.0, **kwargs):
    """POST with bounded retries on transport errors only.

    Use ONLY where the caller has already established that a duplicate write is
    either impossible or detected (see the comment idempotency marker). Retries
    cover ``ConnectionResetError`` / connection-pool exhaustion, which is what
    made 4 of 17 comments fail mid-run on 2026-09-01; an HTTP response of any
    status is returned to the caller untouched rather than retried, because the
    server may have applied the write.
    """
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return session.post(url, **kwargs)
        except requests.RequestException as err:
            last = err
            if attempt == attempts:
                break
            time.sleep(backoff * attempt)
    raise last  # type: ignore[misc]
