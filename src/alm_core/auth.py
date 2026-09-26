"""One authenticated Jazz session, shared by every tool.

Deliberately built on ``requests`` rather than an async HTTP client. The Jazz
form-auth handshake on this estate has a set of hard-won quirks - the login
endpoint differs between TEST and PROD, a successful POST can still leave an
unauthenticated session, and the server answers with an HTML page rather than an
error - and reimplementing that against a different client would be a gratuitous
source of new bugs. The tools call into it through ``asyncio.to_thread``, so the
graph stays async without the transport being rewritten.

Post-login verification is per service and intentionally *not* shared: EWM must
answer with OSLC XML, JTS's ``/whoami`` must name a contributor. A single shared
truthiness check is what let a browser session declare a login it never
performed and attach the login page to eleven production work items.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter

from .errors import AuthenticationError, ServerError, TransportError
from .logging import get_logger

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

log = get_logger("alm.auth")

_RETRY_STATUS = (429, 500, 502, 503, 504)


class CircuitBreaker:
    """Stops hammering a service that is already failing.

    EWM and JTS are shared corporate systems. A retry storm from an autonomous
    agent is an outage other teams experience, so consecutive failures open the
    circuit and every call fails fast until the cooldown elapses.
    """

    def __init__(self, threshold: int = 5, cooldown: float = 60.0, name: str = ""):
        self.threshold = threshold
        self.cooldown = cooldown
        self.name = name
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._failures < self.threshold:
                return False
            if time.monotonic() - self._opened_at >= self.cooldown:
                # Half-open: let one call through to test the water.
                self._failures = self.threshold - 1
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()
                log.warning("circuit_opened", service=self.name,
                            failures=self._failures, cooldown_s=self.cooldown)

    def check(self) -> None:
        if self.is_open:
            raise TransportError(
                f"circuit breaker open for {self.name}; not sending the request",
                context={"service": self.name, "cooldown_s": self.cooldown})


def _retry(total: int) -> Retry:
    kwargs = {
        "total": total, "connect": total, "read": total, "status": total,
        "backoff_factor": 1.5, "status_forcelist": _RETRY_STATUS,
        "raise_on_status": False, "respect_retry_after_header": True,
    }
    try:
        return Retry(allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]), **kwargs)
    except TypeError:  # urllib3 < 1.26
        return Retry(method_whitelist=frozenset(["GET", "HEAD", "OPTIONS"]), **kwargs)


def make_session(settings, retries: int | None = None) -> requests.Session:
    """A session carrying the configured TLS policy, timeouts and GET retries."""
    session = requests.Session()
    session.verify = settings.verify
    adapter = HTTPAdapter(
        max_retries=_retry(retries if retries is not None else settings.http_max_retries),
        pool_maxsize=settings.max_concurrent_writes * 2,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": f"{settings.service_name}/2.0"})
    return session


def _login_endpoints(session: requests.Session, server: str, timeout) -> list[str]:
    """PROD posts to /authenticated/j_security_check, TEST to /auth/j_security_check."""
    discovered = ""
    try:
        probe = session.get(f"{server}/authenticated/identity", allow_redirects=True,
                            timeout=timeout)
        parts = urlsplit(probe.url)
        discovered = urlunsplit((parts.scheme, parts.netloc,
                                 parts.path.rsplit("/", 1)[0] + "/j_security_check", "", ""))
    except requests.RequestException as err:
        log.warning("login_endpoint_probe_failed", server=server, error=str(err))
    return [u for u in dict.fromkeys([discovered, f"{server}/authenticated/j_security_check"])
            if u]


def _auth_failed(response) -> bool:
    seen = [response.url] + [h.headers.get("Location", "") for h in response.history]
    return any("authfailed" in (url or "") for url in seen)


def form_login(session: requests.Session, server: str, user: str, password: str,
               verify_session, *, timeout) -> None:
    """Authenticate, or raise. Never logs the password.

    Raises ``TransportError`` when the server never answered (DNS, proxy, VPN,
    TLS) and ``AuthenticationError`` when it answered and refused - the two
    need opposite fixes, and "authentication failed" for an unreachable host
    sends the operator to reset a password that was never the problem.
    """
    attempted: list[str] = []
    transport_error: Exception | None = None
    answered = rejected = False
    for login_url in _login_endpoints(session, server, timeout):
        attempted.append(login_url)
        try:
            response = session.post(login_url,
                                    data={"j_username": user, "j_password": password},
                                    allow_redirects=True, timeout=timeout)
        except requests.RequestException as err:
            log.warning("login_post_failed", endpoint=login_url, error=str(err))
            transport_error = err
            continue
        answered = True
        if _auth_failed(response):
            # The server has judged the password. Trying the next endpoint would
            # only add a failed attempt towards an account lockout.
            rejected = True
            break
        try:
            if verify_session(session):
                log.info("authenticated", server=server, user=user)
                return
        except requests.RequestException as err:
            log.warning("login_verification_failed", endpoint=login_url, error=str(err))

    context = {"server": server, "user": user, "endpoints_tried": attempted}
    if not answered:
        cause = type(transport_error).__name__ if transport_error else "no login endpoint"
        if isinstance(transport_error, requests.exceptions.SSLError):
            # The host answered; its certificate did not verify. VPN and proxy
            # advice would send the operator the wrong way.
            raise TransportError(
                f"{server} answered but its TLS certificate is not trusted ({cause}). Set "
                "ALM_CA_BUNDLE in .env to the company CA bundle (.pem) - or, on TEST "
                "only, leave ALM_TLS_VERIFY unset to run unverified.", context=context)
        raise TransportError(
            f"cannot reach {server} ({cause}). Check DNS, the VPN, and that the host "
            "is in NO_PROXY - the corporate proxy cannot reach intranet servers.",
            context=context)
    raise AuthenticationError(
        "Jazz form authentication failed: " + (
            "the server rejected the user ID or password" if rejected else
            "the login was accepted but the session did not verify - check that the "
            "account can open this server in a browser"),
        context=context)


def ewm_session_is_live(server: str, timeout):
    """EWM's own proof: a protected OSLC resource answers with XML, not the web UI."""

    def check(session: requests.Session) -> bool:
        response = session.get(f"{server}/process/project-areas",
                               headers={"Accept": "application/xml"}, timeout=timeout)
        content_type = response.headers.get("Content-Type", "").lower()
        return "html" not in content_type and \
            response.text.lstrip()[:5].lower().startswith("<?xml")

    return check


def jts_session_is_live(server: str, timeout):
    """JTS's own proof: /whoami names a contributor resource."""

    def check(session: requests.Session) -> bool:
        response = session.get(f"{server}/whoami", headers={"Accept": "text/plain"},
                               timeout=timeout)
        body = response.text.strip()
        return response.status_code == 200 and body.startswith("http") and "/users/" in body

    return check


class JazzClient:
    """An authenticated session per server, created on demand and reused.

    Re-authenticates once on a 401/403 before giving up, because a Jazz session
    cookie expires long before a long-running provisioning batch finishes.
    """

    def __init__(self, settings, resolver):
        self.settings = settings
        self.resolver = resolver
        self._sessions: dict[str, requests.Session] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def _password(self, refresh: bool = False) -> str:
        return self.resolver.get(self.settings.password_secret_name, refresh=refresh)

    def breaker(self, server: str) -> CircuitBreaker:
        with self._lock:
            if server not in self._breakers:
                self._breakers[server] = CircuitBreaker(
                    self.settings.circuit_breaker_threshold,
                    self.settings.circuit_breaker_cooldown_seconds,
                    name=server)
            return self._breakers[server]

    def session(self, server: str, *, kind: str, refresh: bool = False) -> requests.Session:
        """An authenticated session for EWM ('ewm') or JTS ('jts')."""
        with self._lock:
            existing = self._sessions.get(server)
            if existing is not None and not refresh:
                return existing

        self.settings.require("service_account")
        session = make_session(self.settings)
        verifier = (ewm_session_is_live if kind == "ewm" else jts_session_is_live)(
            server, self.settings.timeout)
        form_login(session, server, self.settings.service_account,
                   self._password(refresh=refresh), verifier,
                   timeout=self.settings.timeout)
        with self._lock:
            self._sessions[server] = session
        return session

    def request(self, method: str, url: str, *, server: str, kind: str, **kwargs):
        """Perform a request, re-authenticating once if the session has expired."""
        breaker = self.breaker(server)
        breaker.check()
        kwargs.setdefault("timeout", self.settings.timeout)
        session = self.session(server, kind=kind)
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as err:
            breaker.record_failure()
            raise TransportError(f"{method} {url} failed: {err}",
                                 context={"server": server}) from err

        if response.status_code in (401, 403):
            log.info("session_expired_reauthenticating", server=server)
            session = self.session(server, kind=kind, refresh=True)
            try:
                response = session.request(method, url, **kwargs)
            except requests.RequestException as err:
                breaker.record_failure()
                raise TransportError(f"{method} {url} failed after re-auth: {err}",
                                     context={"server": server}) from err

        if response.status_code >= 500:
            breaker.record_failure()
            raise ServerError(f"{method} {url} returned HTTP {response.status_code}",
                              context={"server": server, "body": response.text[:300]})

        breaker.record_success()
        return response

    def close(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()
