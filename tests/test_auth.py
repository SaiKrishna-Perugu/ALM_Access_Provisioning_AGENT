"""Sign-in and roles: OIDC against a fake IdP, sessions, CSRF, role mapping."""
from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from alm_api import auth, main  # noqa: E402
from alm_core.store.memory import MemoryStore  # noqa: E402

ISSUER = "https://idp.example.com/tenant"
SECRETS = {"session": "s" * 32, "oidc": "client-secret",  # pragma: allowlist secret
           "webhook": "w"}


def b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


class FakeIdp:
    """Discovery and a token endpoint, issuing an ID token for the last login."""

    def __init__(self):
        self.claims = {"sub": "u-1", "email": "Ops@Example.com", "name": "Ops",
                       "groups": ["alm-operators"]}
        self.nonce = ""
        self.tampered = {}

    def get(self, url, timeout=None):
        assert url == f"{ISSUER}/.well-known/openid-configuration"
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token"})

    def post(self, url, timeout=None, data=None):
        assert url == f"{ISSUER}/token" and data["code"] == "the-code"
        assert data["client_secret"] == "client-secret"  # pragma: allowlist secret
        assert data["code_verifier"]
        claims = {"iss": ISSUER, "aud": "alm-console", "exp": time.time() + 600,
                  "nonce": self.nonce, **self.claims, **self.tampered}
        token = f"{b64({'alg': 'RS256'})}.{b64(claims)}.sig"
        return SimpleNamespace(status_code=200, json=lambda: {"id_token": token})


@pytest.fixture
def oidc_api(monkeypatch):
    idp = FakeIdp()
    monkeypatch.setattr(main.runtime, "services", SimpleNamespace(store=MemoryStore()))
    monkeypatch.setattr(main.runtime, "settings", SimpleNamespace(
        auth_mode="oidc", oidc_issuer=ISSUER, oidc_client_id="alm-console",
        oidc_client_secret_name="oidc", session_secret_name="session",  # pragma: allowlist secret
        oidc_groups_claim="groups", oidc_scopes="openid email", session_hours=8.0,
        role_map='{"alm-operators": "operator", "alm-approvers": "approver"}',
        approval_base_url="https://alm.example.com", environment="TEST", shadow_mode=False,
        webhook_secret_name="webhook"))  # pragma: allowlist secret
    monkeypatch.setattr(main.runtime, "resolver",
                        SimpleNamespace(get=lambda name: SECRETS[name]))
    real = auth.Oidc

    def with_fake_idp(settings, client_secret, http=None):
        return real(settings, client_secret, http=idp)

    monkeypatch.setattr(auth, "Oidc", with_fake_idp)
    client = TestClient(main.app, base_url="https://alm.example.com")
    return client, idp


def sign_in(client, idp):
    start = client.get("/auth/login", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://alm.example.com/auth/callback"]
    idp.nonce = query["nonce"][0]
    return client.get(f"/auth/callback?code=the-code&state={query['state'][0]}",
                      follow_redirects=False)


# ------------------------------------------------------------------- roles

def test_roles_come_from_groups_or_email_and_imply_viewer():
    mapping = {"alm-ops": "operator", "boss@example.com": ["approver"], "*": "viewer",
               "root@example.com": "admin"}
    assert auth.roles_for(groups=["ALM-OPS"], role_map=mapping) == {"operator", "viewer"}
    assert auth.roles_for(email="boss@example.com", role_map=mapping) == {"approver",
                                                                          "viewer"}
    assert auth.roles_for(email="root@example.com", role_map=mapping) == set(auth.ROLES)
    assert auth.roles_for(email="someone@example.com", role_map={}) == set()
    with pytest.raises(ValueError):
        auth.roles_for(email="x@example.com", role_map={"*": "superuser"})


def test_a_session_cannot_be_forged_or_kept_past_expiry():
    user = auth.User(subject="u", email="a@example.com", roles={"viewer"}, csrf="c")
    token = auth.session_for("k" * 32, user, hours=1)
    assert auth.user_from_session("k" * 32, token).roles == {"viewer"}
    _body, mac = token.split(".")
    # The same signature on a body that claims more: refused.
    tampered = f"{b64({'sub': 'u', 'roles': ['admin'], 'exp': time.time() + 60})}.{mac}"
    assert auth.user_from_session("k" * 32, tampered) is None
    assert auth.user_from_session("other-key" * 4, token) is None
    expired = auth.sign("k" * 32, {"sub": "u", "exp": time.time() - 1})
    assert auth.user_from_session("k" * 32, expired) is None


# -------------------------------------------------------------------- OIDC

def test_sign_in_sets_a_strict_session_with_roles(oidc_api):
    client, idp = oidc_api
    done = sign_in(client, idp)
    assert done.status_code == 303 and done.headers["location"] == "/"
    cookie = done.headers["set-cookie"].lower()
    assert "alm_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    me = client.get("/me").json()
    assert me["identity"] == "ops@example.com" and me["roles"] == ["operator", "viewer"]
    assert me["csrf"]


@pytest.mark.parametrize(("tamper", "reason"), [
    ({"iss": "https://evil.example.com"}, "issuer"),
    ({"aud": "someone-else"}, "audience"),
    ({"exp": 1}, "expiry"),
    ({"nonce": "replayed"}, "nonce"),
])
def test_an_id_token_that_fails_a_check_does_not_sign_in(oidc_api, tamper, reason):
    client, idp = oidc_api
    idp.tampered = tamper
    refused = sign_in(client, idp)
    assert refused.status_code == 401 and reason in refused.json()["detail"]
    assert client.get("/me").status_code == 401


def test_a_callback_without_a_matching_login_is_refused(oidc_api):
    client, _idp = oidc_api
    assert client.get("/auth/callback?code=the-code&state=guess").status_code == 400


def test_someone_with_no_role_is_turned_away(oidc_api):
    client, idp = oidc_api
    idp.claims["groups"] = ["unrelated"]
    assert sign_in(client, idp).status_code == 403


def test_changes_need_the_csrf_token_and_the_role(oidc_api):
    client, idp = oidc_api
    sign_in(client, idp)
    csrf = client.get("/me").json()["csrf"]
    body = {"prompt": "dry run 1001"}
    assert client.post("/runs", json=body).status_code == 403            # no CSRF
    assert client.post("/runs", json=body,
                       headers={"x-csrf-token": "wrong"}).status_code == 403
    assert client.post("/runs", json=body, headers={"x-csrf-token": csrf}).status_code == 201
    assert client.post("/admin/reconcile",
                       headers={"x-csrf-token": csrf}).status_code == 403  # not admin


def test_sign_out_ends_the_session(oidc_api):
    client, idp = oidc_api
    sign_in(client, idp)
    client.get("/auth/logout")
    assert client.get("/me").status_code == 401


def test_iap_mode_has_no_sign_in_routes(oidc_api, monkeypatch):
    client, _idp = oidc_api
    monkeypatch.setattr(main.runtime.settings, "auth_mode", "iap")
    assert client.get("/auth/login").status_code == 404
