"""The Windows Kerberos worker: consume AD jobs, drive GPT, report back.

Runs on a domain-joined Windows host as a gMSA or service account. Start it with:

    python -m alm_worker.main

Where jobs come from follows ``ALM_AD_JOB_TRANSPORT``:

* ``store`` (default) - claims ``ad_job`` rows from the shared Postgres job
  queue, the same database as the ledger. Any cloud; nothing else to run.
* ``pubsub`` - pulls from a Google Pub/Sub subscription. Authentication to
  Google is Application Default Credentials; on an on-premises Windows host that
  means **Workload Identity Federation**, so there is no downloaded service
  account key on a machine outside the cloud perimeter.

Contract with the orchestrator:

* **Delivery is at least once.** Pub/Sub guarantees no less; the same job can and
  will arrive twice, and a message whose ack deadline lapses is redelivered even
  though the first attempt may still be running. The idempotency key that travels
  in the message is claimed in the shared ledger before the browser is touched,
  so a redelivery becomes a no-op instead of a second group membership request.
* **The message is acked only after the outcome is recorded.** If the worker dies
  mid-job the message is redelivered, the ledger says the write is in flight, and
  the claim lease decides whether to take it over or wait.
* **Poison messages go to the dead-letter topic with a reason**, rather than being
  retried until the subscription backs up. Somebody has to look at those.

Driving a browser takes longer than the default 10-second ack deadline, so the
subscriber extends the lease while a job runs rather than letting Pub/Sub decide
the worker has died.

The worker never invents a result. "Submitted" is the strongest claim available
here; the orchestrator's permission poll is what confirms the access landed.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time

from alm_core.config import get_settings
from alm_core.errors import ConfigError
from alm_core.logging import bind_run, configure, get_logger
from alm_core.models import AuditEvent, Operation, Outcome, ProvisionResult

from .gpt import GptSession

log = get_logger("alm.worker")

MAX_DELIVERY_ATTEMPTS = 3
HEARTBEAT_SECONDS = 60
REQUIRED_FIELDS = ("idempotency_key", "userid", "group", "domain", "run_id")
# One job at a time: the worker drives a single browser session, so pulling a
# second message concurrently would interleave two GPT flows in one window.
MAX_IN_FLIGHT = 1
# Long enough to open the group, stage a user and submit, with headroom.
ACK_DEADLINE_SECONDS = 600
STORE_POLL_SECONDS = 5.0

_stop = threading.Event()


def _handle_signal(signum, _frame):
    log.info("worker_stopping", signal=signum)
    _stop.set()


def validate_job(body: bytes) -> dict:
    """Parse and validate a job. Raises ValueError for a poison message."""
    try:
        job = json.loads(body)
    except ValueError as err:
        raise ValueError(f"message body is not JSON: {err}") from err
    if not isinstance(job, dict):
        raise ValueError("message body is not a JSON object")
    if job.get("operation") != Operation.AD_GROUP_ADD.value:
        raise ValueError(f"unsupported operation {job.get('operation')!r}")
    missing = [f for f in REQUIRED_FIELDS if not job.get(f)]
    if missing:
        raise ValueError(f"job is missing required fields: {', '.join(missing)}")
    return job


class Worker:
    """Owns the browser session, the ledger connection and the consume loop."""

    def __init__(self, settings):
        self.settings = settings
        self.worker_id = f"gpt-{socket.gethostname()}-{os.getpid()}"
        self.session: GptSession | None = None
        self.store = None
        self._loop = None
        self._processed = 0
        self._failed = 0
        self._last_heartbeat = 0.0

    # ------------------------------------------------------------- plumbing

    def _run_async(self, coro):
        """The Pub/Sub pull client is synchronous; the store is async."""
        import asyncio

        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def start(self) -> None:
        from alm_core.store import get_store

        if not self.settings.postgres_dsn:
            raise ConfigError(
                "the worker needs ALM_POSTGRES_DSN: without the shared ledger it "
                "cannot tell a redelivered job from a new one")
        self.store = self._run_async(get_store(self.settings))
        self.session = GptSession(headless=True)
        self.session.start()
        log.info("worker_started", transport=self.settings.ad_job_transport,
                 subscription=self.settings.pubsub_subscription,
                 environment=self.settings.environment)

    def stop(self) -> None:
        if self.session is not None:
            self.session.close()
        if self.store is not None:
            self._run_async(self.store.close())
        if self._loop is not None:
            self._loop.close()
        log.info("worker_stopped", processed=self._processed, failed=self._failed)

    def heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = now
        log.info("worker_heartbeat", processed=self._processed, failed=self._failed,
                 subscription=self.settings.pubsub_subscription)

    # ------------------------------------------------------------- job path

    def process(self, job: dict) -> ProvisionResult:
        """Perform one AD group addition, guarded by the shared ledger."""
        key = job["idempotency_key"]
        userid = job["userid"]
        work_item_id = job.get("work_item_id", "")
        bind_run(run_id=job.get("run_id", ""), thread_id=job.get("thread_id", ""))

        base = {"userid": userid, "operation": Operation.AD_GROUP_ADD,
                "work_item_id": work_item_id, "idempotency_key": key}

        proceed, previous = self._run_async(self.store.claim(
            key, run_id=job.get("run_id", ""), work_item_id=work_item_id,
            userid=userid, operation=Operation.AD_GROUP_ADD))
        if not proceed:
            log.info("job_already_done", userid=userid, key=key[:12])
            return previous or ProvisionResult(
                **base, outcome=Outcome.SKIPPED, replayed=True,
                message="already completed by an earlier delivery")

        try:
            ok, message = self.session.add_member(
                userid=userid, group=job["group"], domain=job["domain"])
            result = ProvisionResult(**base,
                                     outcome=Outcome.OK if ok else Outcome.FAILED,
                                     message=message)
        except Exception as err:  # noqa: BLE001 - one job must not kill the worker
            log.exception("job_crashed", userid=userid)
            result = ProvisionResult(**base, outcome=Outcome.FAILED,
                                     message=f"{type(err).__name__}: {err}")
            # The browser may be in an unknown state after a crash mid-flow.
            self._restart_browser()

        self._run_async(self.store.complete(key, result))
        self._run_async(self.store.record(AuditEvent.from_result(
            result, run_id=job.get("run_id", ""), thread_id=job.get("thread_id", ""),
            environment=job.get("environment", ""), step="ad_provision",
            approver=job.get("approved_by", ""))))
        return result

    def _restart_browser(self) -> None:
        log.warning("restarting_browser_after_crash")
        try:
            if self.session is not None:
                self.session.close()
        except Exception:  # noqa: S110, BLE001 - ignore errors closing crashed session
            pass
        self.session = GptSession(headless=True)
        self.session.start()

    # ------------------------------------------------------------ consume

    def run(self) -> None:
        """Consume jobs from the configured transport until told to stop."""
        if self.settings.ad_job_transport == "pubsub":
            self.run_pubsub()
        else:
            self.run_store()

    def run_store(self) -> None:
        """Claim ``ad_job`` rows from the shared queue, one at a time.

        The queue's lease plays the ack deadline's part: a worker that dies
        mid-job loses the claim and the job is taken over; one that keeps
        failing goes ``dead`` after MAX_DELIVERY_ATTEMPTS for a human to look at.
        """
        from alm_core.tools.gpt_queue import AD_JOB

        while not _stop.is_set():
            self.heartbeat()
            try:
                job = self._run_async(self.store.claim_job(
                    self.worker_id, ACK_DEADLINE_SECONDS,
                    max_attempts=MAX_DELIVERY_ATTEMPTS, kinds=(AD_JOB,)))
            except Exception as err:  # noqa: BLE001 - the database blipped; try again
                log.warning("claim_failed", error=str(err))
                _stop.wait(5)
                continue
            if job is None:
                _stop.wait(STORE_POLL_SECONDS)
                continue
            self.handle_store_job(job)

    def handle_store_job(self, job: dict) -> None:
        try:
            payload = validate_job(json.dumps(job["payload"]).encode("utf-8"))
        except ValueError as err:
            log.error("poison_message", error=str(err), job=job["id"])
            self._run_async(self.store.finish_job(
                job["id"], self.worker_id, ok=False, error=f"poison: {err}",
                max_attempts=job["attempts"]))   # never retried
            self._failed += 1
            return
        try:
            result = self.process(payload)
        except Exception as err:  # noqa: BLE001 - keep the job for a retry
            log.exception("job_processing_failed", userid=payload.get("userid"))
            self._run_async(self.store.finish_job(
                job["id"], self.worker_id, ok=False, error=f"{type(err).__name__}: {err}",
                max_attempts=MAX_DELIVERY_ATTEMPTS))
            self._failed += 1
            return
        # Closed only after the outcome is durably recorded by process().
        self._run_async(self.store.finish_job(job["id"], self.worker_id, ok=True))
        self._processed += 1
        log.info("job_complete", userid=result.userid, outcome=result.outcome.value,
                 replayed=result.replayed, attempt=job["attempts"])

    def run_pubsub(self) -> None:
        """Pull messages one at a time and process them to completion.

        Synchronous pull rather than the streaming subscriber: this worker owns a
        single browser and must process strictly one job at a time. The streaming
        client's flow control would still deliver into callback threads, which is
        the wrong shape for a resource that cannot be shared.
        """
        from google.api_core import exceptions as gexc
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        subscription = self.settings.subscription_path()
        try:
            while not _stop.is_set():
                self.heartbeat()
                try:
                    response = subscriber.pull(
                        request={"subscription": subscription,
                                 "max_messages": MAX_IN_FLIGHT},
                        timeout=30)
                except gexc.DeadlineExceeded:
                    continue  # an idle queue, not a failure
                except gexc.GoogleAPICallError as err:
                    log.warning("pull_failed", error=str(err))
                    time.sleep(5)
                    continue

                for received in response.received_messages:
                    if _stop.is_set():
                        # Hand it straight back so another worker can take it.
                        subscriber.modify_ack_deadline(
                            request={"subscription": subscription,
                                     "ack_ids": [received.ack_id],
                                     "ack_deadline_seconds": 0})
                        continue
                    self._handle(subscriber, subscription, received)
        finally:
            subscriber.close()

    def _extend_deadline(self, subscriber, subscription: str, ack_id: str) -> None:
        """Keep the lease while the browser flow runs."""
        try:
            subscriber.modify_ack_deadline(
                request={"subscription": subscription, "ack_ids": [ack_id],
                         "ack_deadline_seconds": ACK_DEADLINE_SECONDS})
        except Exception as err:  # noqa: BLE001 - losing the lease is survivable
            log.warning("ack_deadline_extend_failed", error=str(err))

    def _handle(self, subscriber, subscription: str, received) -> None:
        message = received.message
        ack_id = received.ack_id
        body = bytes(message.data)

        try:
            job = validate_job(body)
        except ValueError as err:
            # Poison: no number of retries will make this parse. Acking sends it
            # nowhere, so let the subscription's dead-letter policy take it by
            # nacking - but log loudly, because somebody must look at it.
            log.error("poison_message", error=str(err),
                      message_id=message.message_id,
                      attempt=message.delivery_attempt)
            subscriber.modify_ack_deadline(
                request={"subscription": subscription, "ack_ids": [ack_id],
                         "ack_deadline_seconds": 0})
            self._failed += 1
            return

        attempt = message.delivery_attempt or 1
        if attempt > MAX_DELIVERY_ATTEMPTS:
            # The dead-letter policy will move it after max_delivery_attempts;
            # this is the belt to that braces, and it stops a slow poison job
            # from occupying the only browser.
            log.error("giving_up_after_retries", userid=job.get("userid"),
                      attempts=attempt)
            subscriber.modify_ack_deadline(
                request={"subscription": subscription, "ack_ids": [ack_id],
                         "ack_deadline_seconds": 0})
            self._failed += 1
            return

        self._extend_deadline(subscriber, subscription, ack_id)
        try:
            result = self.process(job)
        except Exception:  # noqa: BLE001 - keep the message for redelivery
            log.exception("job_processing_failed", userid=job.get("userid"))
            subscriber.modify_ack_deadline(
                request={"subscription": subscription, "ack_ids": [ack_id],
                         "ack_deadline_seconds": 0})
            self._failed += 1
            return

        # Acked only after the outcome is durably recorded by process().
        subscriber.acknowledge(
            request={"subscription": subscription, "ack_ids": [ack_id]})
        self._processed += 1
        log.info("job_complete", userid=result.userid, outcome=result.outcome.value,
                 replayed=result.replayed, attempt=attempt)


def main() -> int:
    configure()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    settings = get_settings()
    if settings.ad_job_transport == "pubsub" and not settings.project_id:
        log.error("worker_misconfigured",
                  error="GOOGLE_CLOUD_PROJECT is required to pull from Pub/Sub")
        return 2
    worker = Worker(settings)
    try:
        worker.start()
        worker.run()
    except ConfigError as err:
        log.error("worker_misconfigured", error=err.message)
        return 2
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
