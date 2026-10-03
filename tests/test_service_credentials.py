"""Services never prompt, workers refuse to start without credentials, and the
status page probes Jazz without risking an account lockout."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from alm_agents import worker  # noqa: E402
from alm_api import main  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.credentials import build_resolver  # noqa: E402
from alm_core.errors import ConfigError  # noqa: E402
from alm_core.store.memory import MemoryStore  # noqa: E402

VIEW = {"x-goog-authenticated-user-email": "accounts.google.com:view@example.com"}


def settings(**extra) -> Settings:
    base = {"_env_file": None, "environment": "TEST", "orchestration": "guided",
            "llm_enabled": True, "shadow_mode": True,
            "ewm_server": "https://ewm.example.com/ccm",
            "jts_server": "https://jts.example.com/jts"}
    base.update(extra)
    return Settings(**base)


def test_a_service_resolver_never_prompts():
    resolver = build_resolver(settings(), interactive=False)
    assert "interactive" not in [p.name for p in resolver.providers]


def test_a_worker_without_its_service_account_refuses_to_start(monkeypatch):
    monkeypatch.delenv("EWM_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="missing required configuration: CID$"):
        worker.preflight(settings(), build_resolver(settings(), interactive=False))
    with_cid = settings().model_copy(update={"service_account": "svc-alm"})
    with pytest.raises(ConfigError, match="no password for the service account svc-alm"):
        worker.preflight(with_cid, build_resolver(with_cid, interactive=False))
    monkeypatch.setenv("EWM_PASSWORD", "pw")  # pragma: allowlist secret
    worker.preflight(with_cid, build_resolver(with_cid, interactive=False))


class Client:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def session(self, server, kind):
        self.calls.append(kind)
        if kind in self.fail:
            raise RuntimeError("the server rejected the user ID or password")
        return object()


@pytest.fixture
def status_api(monkeypatch):
    monkeypatch.delenv("ALM_IAP_AUDIENCE", raising=False)
    main._status.clear()

    def build(client, llm="model"):
        monkeypatch.setattr(main.runtime, "services", SimpleNamespace(
            store=MemoryStore(), client=client, agent_llm=llm))
        monkeypatch.setattr(main.runtime, "settings", settings(
            auth_mode="iap", role_map='{"view@example.com": "viewer"}'))
        return TestClient(main.app)

    yield build
    main._status.clear()


def test_status_reports_every_dependency(status_api):
    body = status_api(Client()).get("/status", headers=VIEW).json()
    assert body["ok"] and set(body["checks"]) == {"database", "ewm", "jts", "model"}
    assert body["checks"]["ewm"]["detail"] == "ewm.example.com"


def test_a_failed_sign_in_is_not_retried_for_ten_minutes(status_api):
    client = Client(fail={"jts"})
    api = status_api(client)
    first = api.get("/status", headers=VIEW).json()
    second = api.get("/status", headers=VIEW).json()
    assert not first["ok"] and "rejected" in first["checks"]["jts"]["detail"]
    assert second["checks"]["jts"] == first["checks"]["jts"]
    assert client.calls.count("jts") == 1          # no second sign-in attempt
    assert status_api(Client()).get("/status").status_code == 401


def test_a_missing_model_is_reported(status_api):
    body = status_api(Client(), llm=None).get("/status", headers=VIEW).json()
    assert not body["checks"]["model"]["ok"]
