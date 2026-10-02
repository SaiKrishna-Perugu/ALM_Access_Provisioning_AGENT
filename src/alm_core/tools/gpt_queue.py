"""Hand AD group changes to the Windows Kerberos worker.

How the job travels is configuration (``ALM_AD_JOB_TRANSPORT``):

* ``store`` (default) - a row in the shared Postgres job queue (kind
  ``ad_job``). Works on any cloud; the worker already needs that database for
  the ledger, so nothing else has to exist.
* ``pubsub`` - a Google Pub/Sub message, for the original GCP deployment.

GPT provisioning is the one step that cannot run in the Linux container: it
needs Windows Kerberos SSO through a real browser process. So the orchestrator
does not perform it - it publishes a job and records that it did.

Two things this module is careful about:

* **A queued job is not a completed job.** GPT itself queues the AD change
  asynchronously, so even the worker cannot confirm membership synchronously.
  The recorded outcome is "submitted for provisioning", and the JazzUsers
  permission poll is what eventually proves the access landed. The old code
  asserted membership by re-reading a staging grid that Modify had just
  cleared, and reported ten false failures while GPT itself said zero.
* **The idempotency key travels with the job**, both in the payload and as a
  Pub/Sub message attribute. Pub/Sub guarantees at-least-once delivery, not
  exactly-once, so the worker claims the same key in the shared ledger before it
  acts - a redelivered message cannot add a user twice.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..errors import ConfigError, WorkerUnavailable
from ..logging import get_logger
from ..models import Operation, ProvisionResult, RequestedUser, idempotency_key
from .base import ToolContext, guarded_write, to_thread

log = get_logger("alm.tools.gpt")

JOB_SCHEMA = 1
AD_JOB = "ad_job"
# The worker's own ledger key. The orchestrator records "submitted" under the
# plain key; if the worker claimed that same key it would find it completed and
# never touch GPT. Its key is distinct, and just as stable across redeliveries.
WORKER_KEY_VARIANT = "gpt-worker"


def build_job(*, run_id: str, thread_id: str, userid: str, work_item_id: str,
              group: str, domain: str, approver: str, environment: str) -> dict:
    """The message body the Windows worker consumes."""
    return {
        "schema": JOB_SCHEMA,
        "operation": Operation.AD_GROUP_ADD.value,
        "idempotency_key": idempotency_key(work_item_id, userid, Operation.AD_GROUP_ADD,
                                           WORKER_KEY_VARIANT),
        "run_id": run_id,
        "thread_id": thread_id,
        "environment": environment,
        "userid": userid,
        "work_item_id": work_item_id,
        "group": group,
        "domain": domain,
        "approved_by": approver,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }


class PubSubPublisher:
    """Thin wrapper so the SDK import stays lazy and the client is reused.

    A publisher client holds a gRPC channel and a batching thread; creating one
    per message would be both slow and a file-descriptor leak.
    """

    def __init__(self, settings):
        self.settings = settings
        self._client = None
        self._topic = ""

    def _ensure(self):
        if self._client is not None:
            return self._client
        try:
            from google.cloud import pubsub_v1
        except ImportError as err:  # pragma: no cover
            raise ConfigError(
                "google-cloud-pubsub is required to publish AD jobs "
                "(pip install -r requirements-cloud.txt)") from err
        if not self.settings.project_id:
            raise ConfigError(
                "set GOOGLE_CLOUD_PROJECT to publish AD jobs to Pub/Sub")
        self._client = pubsub_v1.PublisherClient()
        self._topic = self.settings.topic_path()
        return self._client

    def publish(self, job: dict) -> str:
        client = self._ensure()
        # Attributes let the subscriber filter and let an operator identify a
        # message in the console without decoding the body.
        future = client.publish(
            self._topic,
            json.dumps(job).encode("utf-8"),
            idempotency_key=job["idempotency_key"],
            operation=job["operation"],
            environment=job.get("environment", ""),
            userid=job["userid"],
        )
        # Block on the publish so a failure surfaces here, inside guarded_write,
        # rather than silently in a background thread after the run reported OK.
        return future.result(timeout=30)

    def close(self) -> None:
        self._client = None


_publisher: PubSubPublisher | None = None


def get_publisher(settings) -> PubSubPublisher:
    global _publisher
    if _publisher is None:
        _publisher = PubSubPublisher(settings)
    return _publisher


def _publish(ctx: ToolContext, job: dict) -> tuple[bool, str, dict]:
    try:
        message_id = get_publisher(ctx.settings).publish(job)
    except ConfigError:
        raise
    except Exception as err:  # SDK transport failures are varied and retryable
        raise WorkerUnavailable(f"could not publish the AD job: {err}",
                                context={"topic": ctx.settings.pubsub_topic}) from err
    return True, ("submitted for AD provisioning; membership is confirmed later by the "
                  "JazzUsers permission check"), {"message_id": message_id}


async def _enqueue(ctx: ToolContext, job: dict) -> tuple[bool, str, dict]:
    """Queue the job in the shared store. The idempotency key is the job's thread,
    so a second publish of the same addition is a no-op while the first waits."""
    job_id = await ctx.store.enqueue_job(AD_JOB, job["idempotency_key"], job, dedupe=True)
    return True, ("submitted for AD provisioning; membership is confirmed later by the "
                  "JazzUsers permission check"), {"job_id": job_id, "transport": "store"}


async def request_group_membership(ctx: ToolContext, user: RequestedUser, *,
                                   group: str, domain: str) -> ProvisionResult:
    """Hand one AD group addition to the Windows worker."""
    work_item_id = user.work_item_ids[0] if user.work_item_ids else ""
    job = build_job(run_id=ctx.run_id, thread_id=ctx.thread_id, userid=user.userid,
                    work_item_id=work_item_id, group=group, domain=domain,
                    approver=ctx.approver, environment=ctx.environment)
    if getattr(ctx.settings, "ad_job_transport", "store") == "pubsub":
        action = lambda: to_thread(_publish, ctx, job)  # noqa: E731
    else:
        action = lambda: _enqueue(ctx, job)  # noqa: E731
    return await guarded_write(
        ctx, userid=user.userid, work_item_id=work_item_id,
        operation=Operation.AD_GROUP_ADD, step="ad_provision", action=action)
