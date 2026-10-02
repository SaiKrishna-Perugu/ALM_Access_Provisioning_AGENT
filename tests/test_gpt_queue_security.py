"""Tests for alm_core.tools.gpt_queue: data minimization and idempotency guarantees."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# alm_core needs pydantic: these run in the agents CI job, and skip in the
# CLI-only test job that installs requirements.txt alone.
pytest.importorskip("pydantic")

from alm_core.models import Operation, idempotency_key  # noqa: E402
from alm_core.tools.gpt_queue import PubSubPublisher, build_job  # noqa: E402


def test_build_job_payload_contains_no_personal_data_beyond_userid():
    job = build_job(
        run_id="run-123",
        thread_id="th-456",
        userid="AB12345",
        work_item_id="9999",
        group="CN=ALM_Users,OU=Groups,DC=example,DC=com",
        domain="example.com",
        approver="approver@example.com",
        environment="TEST",
    )

    # Allowed keys in schema
    expected_keys = {
        "schema",
        "operation",
        "idempotency_key",
        "run_id",
        "thread_id",
        "environment",
        "userid",
        "work_item_id",
        "group",
        "domain",
        "approved_by",
        "enqueued_at",
    }
    assert set(job.keys()) == expected_keys

    # Confirm sensitive/PII attributes are not present in the payload
    disallowed_fields = [
        "name",
        "first_name",
        "last_name",
        "email",
        "password",
        "token",
        "ssn",
        "phone",
    ]
    for field in disallowed_fields:
        assert field not in job


def test_build_job_idempotency_key_is_deterministic():
    job1 = build_job(
        run_id="run-1",
        thread_id="th-1",
        userid="AB12345",
        work_item_id="9999",
        group="ALM_GROUP",
        domain="example.com",
        approver="approver@example.com",
        environment="TEST",
    )
    job2 = build_job(
        run_id="run-2",  # different run
        thread_id="th-2",  # different thread
        userid="AB12345",
        work_item_id="9999",
        group="ALM_GROUP",
        domain="example.com",
        approver="approver@example.com",
        environment="TEST",
    )

    expected_key = idempotency_key("9999", "AB12345", Operation.AD_GROUP_ADD, "gpt-worker")
    assert job1["idempotency_key"] == expected_key
    assert job2["idempotency_key"] == expected_key
    assert job1["idempotency_key"] == job2["idempotency_key"]


def test_pubsub_publisher_publishes_idempotency_attributes():
    settings = MagicMock()
    settings.project_id = "test-project"
    settings.topic_path.return_value = "projects/test-project/topics/alm-ad-jobs"

    publisher = PubSubPublisher(settings)
    mock_client = MagicMock()
    publisher._client = mock_client
    publisher._topic = settings.topic_path()
    mock_future = MagicMock()
    mock_future.result.return_value = "pubsub-msg-12345"
    mock_client.publish.return_value = mock_future

    job = build_job(
        run_id="run-1",
        thread_id="th-1",
        userid="AB12345",
        work_item_id="9999",
        group="ALM_GROUP",
        domain="example.com",
        approver="approver@example.com",
        environment="TEST",
    )

    msg_id = publisher.publish(job)
    assert msg_id == "pubsub-msg-12345"

    mock_client.publish.assert_called_once()
    call_args, call_kwargs = mock_client.publish.call_args
    assert call_args[0] == "projects/test-project/topics/alm-ad-jobs"
    assert call_kwargs["idempotency_key"] == job["idempotency_key"]
    assert call_kwargs["operation"] == Operation.AD_GROUP_ADD.value
    assert call_kwargs["userid"] == "AB12345"
