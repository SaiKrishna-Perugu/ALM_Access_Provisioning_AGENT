"""Verification test suite for engineering review remediation plan.

Covers:
1. Session expiry resilience (HardenedJazzClient & JazzClient transparent reauth).
2. OSLC ETag (HTTP 412) conflict auto-retries.
3. Distributed concurrency / lease locking across workers.
4. Deterministic LLM extraction guardrails & privilege escalation prevention.
5. PII and secret redaction filter for logs and telemetry.
6. Step-level idempotency key derivation and cached execution.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
import requests

from alm_agents.nodes.provisioning import execute_idempotent_step, make_step_key
from alm_core.auth import AUTH_HEADER_KEY, AUTH_REQUIRED_VAL, is_auth_expired
from alm_core.logging import PIIRedactingFilter, scrub_pii_and_secrets
from alm_core.models import (
    ExtractedAccessRequest,
    JTSLicenseTier,
    RepositoryPermission,
)
from jazz_client import HardenedJazzClient

# ---------------------------------------------------------------------------
# 1. Session Expiry Resilience Test
# ---------------------------------------------------------------------------

def test_session_expiry_detection_authrequired_header():
    resp = requests.Response()
    resp.status_code = 200
    resp.headers[AUTH_HEADER_KEY] = AUTH_REQUIRED_VAL
    assert is_auth_expired(resp) is True


def test_session_expiry_detection_html_login_page():
    resp = requests.Response()
    resp.status_code = 200
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp._content = b"<html><form action='j_security_check' method='POST'></form></html>"
    assert is_auth_expired(resp) is True


def test_session_expiry_detection_401_403():
    for code in (401, 403):
        resp = requests.Response()
        resp.status_code = code
        assert is_auth_expired(resp) is True


def test_hardened_jazz_client_transparent_reauth(monkeypatch):
    client = HardenedJazzClient("https://jazz.example.com", "user", "pass", verify_ssl=False)
    auth_calls = 0

    def mock_auth():
        nonlocal auth_calls
        auth_calls += 1

    client.authenticate = mock_auth

    # First response: 200 OK with authrequired header
    expired_resp = requests.Response()
    expired_resp.status_code = 200
    expired_resp.headers[AUTH_HEADER_KEY] = AUTH_REQUIRED_VAL

    # Second response: 200 OK with normal json
    ok_resp = requests.Response()
    ok_resp.status_code = 200
    ok_resp.headers["Content-Type"] = "application/json"
    ok_resp._content = b'{"status": "success"}'

    call_count = 0

    def mock_request(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return expired_resp
        return ok_resp

    monkeypatch.setattr(client.session, "request", mock_request)

    resp = client.request("GET", "https://jazz.example.com/oslc/workitems/12345")
    assert resp.status_code == 200
    assert auth_calls == 1
    assert call_count == 2
    assert resp.json() == {"status": "success"}


# ---------------------------------------------------------------------------
# 2. ETag 412 Conflict Test
# ---------------------------------------------------------------------------

def test_hardened_jazz_client_etag_412_conflict_retry(monkeypatch):
    client = HardenedJazzClient("https://jazz.example.com", "user", "pass", verify_ssl=False)

    get_resp_1 = requests.Response()
    get_resp_1.status_code = 200
    get_resp_1.headers["ETag"] = '"tag1"'
    get_resp_1._content = b"<xml>initial</xml>"

    get_resp_2 = requests.Response()
    get_resp_2.status_code = 200
    get_resp_2.headers["ETag"] = '"tag2"'
    get_resp_2._content = b"<xml>updated_by_someone_else</xml>"

    put_resp_412 = requests.Response()
    put_resp_412.status_code = 412

    put_resp_200 = requests.Response()
    put_resp_200.status_code = 200
    put_resp_200._content = b"<xml>saved</xml>"

    get_count = 0
    put_count = 0

    def mock_req(method, url, **kwargs):
        nonlocal get_count, put_count
        if method == "GET":
            get_count += 1
            return get_resp_1 if get_count == 1 else get_resp_2
        if method == "PUT":
            put_count += 1
            if put_count == 1:
                err = requests.HTTPError("412 Precondition Failed", response=put_resp_412)
                raise err
            return put_resp_200
        raise ValueError(f"Unexpected method {method}")

    client.request = mock_req

    result = client.update_oslc_resource_with_etag(
        "https://jazz.example.com/oslc/resource/1",
        mutate_fn=lambda body, etag: body + f":mutated_with_{etag}".encode(),
        max_conflict_retries=3,
    )

    assert result.status_code == 200
    assert get_count == 2
    assert put_count == 2


# ---------------------------------------------------------------------------
# 3. Multi-Worker Distributed Concurrency Test
# ---------------------------------------------------------------------------

def test_multi_worker_distributed_lease_concurrency(tmp_path):
    from alm_core.store.sqlite import SqliteStore

    async def scenario():
        store = SqliteStore(str(tmp_path / "test.db"))
        await store.start()
        await store.migrate()

        winners = []

        async def worker_attempt(worker_id: str):
            acquired = await store.acquire_distributed_lock("lock:wi-1001", worker_id, ttl_seconds=60)
            if acquired:
                winners.append(worker_id)

        tasks = [worker_attempt(f"worker-{i}") for i in range(5)]
        await asyncio.gather(*tasks)

        assert len(winners) == 1
        assert winners[0] in [f"worker-{i}" for i in range(5)]

        await store.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Privilege Escalation Guard & Pydantic Validation Test
# ---------------------------------------------------------------------------

def test_privilege_escalation_blocks_jazzadmins():
    # JazzAdmins is explicitly omitted from RepositoryPermission
    with pytest.raises(ValueError, match="JazzAdmins"):
        RepositoryPermission("JazzAdmins")


def test_extracted_access_request_valid():
    req = ExtractedAccessRequest(
        workitem_id=1234,
        user_id="JD12345",
        email="john.doe@example.com",
        full_name="John Doe",
        project_area="Engineering Core",
        requested_roles=["Developer"],
        license_tier=JTSLicenseTier.EWM_DEVELOPER,
        repo_permission=RepositoryPermission.JAZZ_USERS,
        extraction_confidence=0.95,
    )
    assert req.user_id == "JD12345"
    assert req.license_tier == JTSLicenseTier.EWM_DEVELOPER
    assert req.repo_permission == RepositoryPermission.JAZZ_USERS


def test_extracted_access_request_rejects_malformed_user_id():
    with pytest.raises(ValueError, match="Invalid User ID format"):
        ExtractedAccessRequest(
            workitem_id=1234,
            user_id="bad user id with spaces",
            email="user@example.com",
            full_name="Test User",
            project_area="Core",
            requested_roles=["Developer"],
            extraction_confidence=0.9,
        )


def test_extracted_access_request_rejects_script_and_xml_injection():
    # Prompt injection / XSS attempt in full_name
    with pytest.raises(ValueError, match="Forbidden control or markup characters"):
        ExtractedAccessRequest(
            workitem_id=1234,
            user_id="AB12345",
            email="user@example.com",
            full_name="<script>alert(1)</script>",
            project_area="Core",
            requested_roles=["Developer"],
            extraction_confidence=0.9,
        )

    # SQL/XML control characters in project_area
    with pytest.raises(ValueError, match="Forbidden control or markup characters"):
        ExtractedAccessRequest(
            workitem_id=1234,
            user_id="AB12345",
            email="user@example.com",
            full_name="Valid Name",
            project_area="Core & Admin; DROP TABLE users;",
            requested_roles=["Developer"],
            extraction_confidence=0.9,
        )


def test_extracted_access_request_rejects_invalid_license_tier():
    with pytest.raises(ValueError):
        ExtractedAccessRequest(
            workitem_id=1234,
            user_id="AB12345",
            email="user@example.com",
            full_name="Valid Name",
            project_area="Core",
            requested_roles=["Developer"],
            license_tier="com.ibm.team.unauthorized.root",
            extraction_confidence=0.9,
        )


# ---------------------------------------------------------------------------
# 5. Log & Trace Sanitization Check
# ---------------------------------------------------------------------------

def test_scrub_pii_and_secrets():
    raw = (
        "Failed login with j_password=mysecretpassword and LtpaToken2: mytokenvalue "
        "for user test.user@example.com and authorization: Bearer secret-token"
    )
    scrubbed = scrub_pii_and_secrets(raw)
    assert "mysecretpassword" not in scrubbed  # pragma: allowlist secret
    assert "mytokenvalue" not in scrubbed  # pragma: allowlist secret
    assert "test.user@example.com" not in scrubbed
    assert "secret-token" not in scrubbed  # pragma: allowlist secret
    assert "[REDACTED_SECRET]" in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed


def test_pii_redacting_filter():
    filt = PIIRedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg="Error authenticating j_password=mysecret for user@example.com",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "mysecret" not in record.msg  # pragma: allowlist secret
    assert "user@example.com" not in record.msg
    assert "[REDACTED_SECRET]" in record.msg
    assert "[REDACTED_EMAIL]" in record.msg


def test_pii_redacting_filter_args():
    filt = PIIRedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg="HTTPError: %s on %s",
        args=("j_password=topsecret", "admin@example.com"),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "topsecret" not in str(record.args)  # pragma: allowlist secret
    assert "admin@example.com" not in str(record.args)


# ---------------------------------------------------------------------------
# 6. Step Idempotency Test
# ---------------------------------------------------------------------------

def test_make_step_key_is_deterministic():
    key1 = make_step_key(1001, "AB12345", "unarchive")
    key2 = make_step_key("1001", "ab12345", "unarchive")
    assert key1 == key2
    assert len(key1) == 64


def test_execute_idempotent_step_executes_and_caches():
    class InMemoryStore:
        def __init__(self):
            self.records = {}

        def get_idempotency_record(self, key):
            return self.records.get(key)

        def save_idempotency_record(self, step_key, workitem_id, user_id, step_name, status, result):
            self.records[step_key] = {
                "key": step_key,
                "workitem_id": workitem_id,
                "user_id": user_id,
                "step_name": step_name,
                "status": status,
                "result": result,
            }

    store = InMemoryStore()
    action_mock = MagicMock(return_value={"unarchived": True})

    # Call 1: executes action
    res1 = execute_idempotent_step(store, 1001, "AB12345", "unarchive", action_mock)
    assert res1 == {"unarchived": True}
    assert action_mock.call_count == 1

    # Call 2: skips action and returns cached result
    res2 = execute_idempotent_step(store, 1001, "AB12345", "unarchive", action_mock)
    assert res2 == {"skipped": True, "cached_result": {"unarchived": True}}
    assert action_mock.call_count == 1
