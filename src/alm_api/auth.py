"""Who is calling, and what they may do.

Two ways to know who someone is (``ALM_AUTH_MODE``):

* ``iap`` - an identity-aware proxy in front of the service (Google IAP)
  has already signed them in; :func:`alm_api.security.caller_identity` reads
  and, with ``ALM_IAP_AUDIENCE``, verifies what it asserts.
* ``oidc`` - the service signs people in itself against the company IdP
  (Entra ID, Okta, Ping, ...): authorization code flow with PKCE, state and
  nonce. The ID token is taken from the token endpoint over a TLS back-channel
  authenticated with the client secret, so - as OpenID Connect Core 3.1.3.7
  allows - TLS vouches for the issuer; its issuer, audience, expiry and nonce
  are still checked here. The result is an HttpOnly session cookie signed
  with a key every replica shares, carrying a CSRF token.

Then the same roles either way, from ``ALM_ROLE_MAP`` (IdP group or e-mail ->
role):

    viewer    see runs, the queue, traces (personal data masked)
    operator  start and stop runs
    approver  decide approval cards (never on a run they started, in PROD)
    auditor   full traces and audit trails, unmasked
    admin     everything, including the manual sweep
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests

from alm_core.logging import get_logger

log = get_logger("alm.auth")

ROLES = ("viewer", "operator", "approver", "auditor", "admin")
# What each role also grants.
IMPLIES = {"admin": set(ROLES), "operator": {"viewer"}, "approver": {"viewer"},
           "auditor": {"viewer"}, "viewer": set()}
SESSION_COOKIE = "alm_session"
LOGIN_COOKIE = "alm_login"
LOGIN_SECONDS = 600


@dataclass
class User:
    subject: str
    email: str = ""
    name: str = ""
    roles: set[str] = field(default_factory=set)
    csrf: str = ""

    @property
    def identity(self) -> str:
        return self.email or self.subject

    def has(self, role: str) -> bool:
        return role in self.roles

    def public(self) -> dict:
        return {"identity": self.identity, "name": self.name, "roles": sorted(self.roles)}


# -------------------------------------------------------------------- roles

def roles_for(*, email: str = "", groups: list[str] | None = None,
              role_map: str | dict = "{}") -> set[str]:
    """Every role the person holds, with what each implies."""
    mapping = json.loads(role_map) if isinstance(role_map, str) else dict(role_map)
    if not isinstance(mapping, dict):
        raise ValueError("ALM_ROLE_MAP must be a JSON object")
    granted = set()
    keys = {g.lower() for g in groups or []} | ({email.lower()} if email else set()) | {"*"}
    for key, value in mapping.items():
        if key.lower() in keys:
            for role in value if isinstance(value, list) else [value]:
                if role not in ROLES:
                    raise ValueError(f"unknown role {role!r} in ALM_ROLE_MAP")
                granted.add(role)
    expanded = set(granted)
    for role in granted:
        expanded |= IMPLIES[role]
    return expanded


# ----------------------------------------------------------------- sessions

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(secret: str, token: str) -> dict | None:
    """The payload if the signature holds and it has not expired, else None."""
    try:
        body, mac = token.split(".", 1)
    except (AttributeError, ValueError):
        return None
    expected = _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, mac):
        return None
    try:
        payload = json.loads(_b64d(body))
    except ValueError:
        return None
    if float(payload.get("exp", 0)) < time.time():
        return None
    return payload


def session_for(secret: str, user: User, hours: float) -> str:
    return sign(secret, {"sub": user.subject, "email": user.email, "name": user.name,
                         "roles": sorted(user.roles), "csrf": user.csrf or
                         secrets.token_urlsafe(24), "exp": time.time() + hours * 3600})


def user_from_session(secret: str, token: str) -> User | None:
    payload = unsign(secret, token or "")
    if payload is None:
        return None
    return User(subject=payload["sub"], email=payload.get("email", ""),
                name=payload.get("name", ""), roles=set(payload.get("roles", [])),
                csrf=payload.get("csrf", ""))


# --------------------------------------------------------------------- OIDC

def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = _b64e(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class Oidc:
    """The authorization code flow against one issuer."""

    def __init__(self, settings, client_secret: str, http: requests.Session | None = None):
        self.settings = settings
        self.client_secret = client_secret
        self.http = http or requests.Session()
        self._config: dict | None = None

    def config(self) -> dict:
        if self._config is None:
            url = self.settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            response = self.http.get(url, timeout=15)
            response.raise_for_status()
            self._config = response.json()
            if self._config.get("issuer", "").rstrip("/") != \
                    self.settings.oidc_issuer.rstrip("/"):
                raise ValueError("the IdP's discovery document names another issuer")
        return self._config

    def start(self, redirect_uri: str) -> tuple[str, dict]:
        """Where to send the browser, and what to remember until it comes back."""
        verifier, challenge = _pkce()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        query = urlencode({
            "response_type": "code", "client_id": self.settings.oidc_client_id,
            "redirect_uri": redirect_uri, "scope": self.settings.oidc_scopes,
            "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256"})
        login = {"state": state, "nonce": nonce, "verifier": verifier,
                 "exp": time.time() + LOGIN_SECONDS}
        return f"{self.config()['authorization_endpoint']}?{query}", login

    def finish(self, *, code: str, redirect_uri: str, login: dict) -> dict:
        """Exchange the code; return the ID token's claims, checked."""
        response = self.http.post(self.config()["token_endpoint"], timeout=15, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.settings.oidc_client_id, "client_secret": self.client_secret,
            "code_verifier": login["verifier"]})
        if response.status_code != 200:
            raise PermissionError(f"the IdP refused the code (HTTP {response.status_code})")
        id_token = response.json().get("id_token", "")
        try:
            claims = json.loads(_b64d(id_token.split(".")[1]))
        except (IndexError, ValueError) as err:
            raise PermissionError("the IdP returned no readable ID token") from err
        now = time.time()
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        problems = [
            (claims.get("iss", "").rstrip("/") != self.config()["issuer"].rstrip("/"),
             "issuer"),
            (self.settings.oidc_client_id not in audiences, "audience"),
            (float(claims.get("exp", 0)) < now, "expiry"),
            (claims.get("nonce") != login.get("nonce"), "nonce"),
        ]
        failed = [name for bad, name in problems if bad]
        if failed:
            raise PermissionError(f"the ID token failed its checks: {', '.join(failed)}")
        return claims

    def user(self, claims: dict) -> User:
        groups = claims.get(self.settings.oidc_groups_claim) or []
        email = (claims.get("email") or claims.get("preferred_username") or "").lower()
        return User(subject=str(claims.get("sub", "")), email=email,
                    name=claims.get("name", ""),
                    roles=roles_for(email=email, groups=list(groups),
                                    role_map=self.settings.role_map),
                    csrf=secrets.token_urlsafe(24))
