"""OpenTelemetry: each run as a tree of spans, and the service's metrics, over OTLP.

Off unless ``ALM_OTEL_ENABLED=true``. The exporter is OTLP over HTTP, which
every tracing backend accepts: Cloud Trace and Managed Prometheus through an
OpenTelemetry collector, AWS X-Ray and CloudWatch through ADOT, Azure Monitor,
Honeycomb, Datadog, Grafana. Where it goes is the standard
``OTEL_EXPORTER_OTLP_ENDPOINT`` (and ``OTEL_EXPORTER_OTLP_HEADERS``); nothing
here is specific to a cloud.

The spans are built from a run's trace records (``alm_agents.trace.RunTrace``)
rather than by instrumenting each library, so they show exactly what the
operator's Trace tab shows::

    run <thread>                    one per job: start, resume, sweep
      agent <name>                  one per supervisor hop
        gen_ai chat <model>         a model call, with GenAI usage attributes
        tool <name>                 a tool call: denied or not, write or read
        ewm/jts/gpt <method>        a backend call
        http <method> <host>        each HTTP exchange
      ledger events                 claim, write, replay, refusal

Only an allowlist of attributes is exported: names, counts, outcomes and
timings. Prompts, replies, tool arguments and results, user IDs, URL paths and
error messages never leave the process - only an error's type does.

Without the SDK installed (``pip install '.[otel]'``) every call is a no-op.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from .logging import get_logger

log = get_logger("alm.telemetry")

# Record fields exported as span attributes, under the alm. prefix. Anything
# else in a record stays in the run's own trace.
_SAFE_FIELDS = ("service", "kind", "agent", "tool", "system", "status", "ok", "denied",
                "write", "operation", "outcome", "replayed", "step", "source", "hop",
                "next", "attempt", "worker", "outcome_unknown", "fallback")
_SPAN_SERVICES = {"ewm", "jts", "gpt", "browser", "auth", "directory"}

_telemetry: Telemetry | None = None
_lock = threading.Lock()


def get() -> Telemetry | None:
    """The process's telemetry, or None when it is off."""
    return _telemetry


def setup(settings, *, service: str, span_exporter=None, metric_reader=None
          ) -> Telemetry | None:
    """Turn telemetry on for this process, once. Returns None when it is off.

    Tests pass an in-memory exporter and reader; production uses OTLP.
    """
    global _telemetry
    if not getattr(settings, "otel_enabled", False) and span_exporter is None:
        return None
    with _lock:
        if _telemetry is not None:
            return _telemetry
        try:
            _telemetry = Telemetry(settings, service=service, span_exporter=span_exporter,
                                   metric_reader=metric_reader)
        except ImportError:
            log.warning("otel_unavailable",
                        reason="ALM_OTEL_ENABLED is set but the OpenTelemetry SDK is not "
                               "installed: pip install '.[otel]'")
            return None
        log.info("otel_enabled", service=service)
        return _telemetry


def shutdown() -> None:
    """Flush and stop. Safe when telemetry is off."""
    global _telemetry
    with _lock:
        if _telemetry is not None:
            _telemetry.shutdown()
            _telemetry = None


class Telemetry:
    """A tracer, a meter and the service's instruments."""

    def __init__(self, settings, *, service: str, span_exporter=None, metric_reader=None):
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

        self.provider_name = str(getattr(settings, "llm_provider", "") or "")
        resource = Resource.create({
            "service.name": service,
            "service.namespace": "alm",
            "service.version": _code_version(),
            "deployment.environment.name": str(getattr(settings, "environment", "") or ""),
        })

        if span_exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            processor = BatchSpanProcessor(OTLPSpanExporter())
        else:
            processor = SimpleSpanProcessor(span_exporter)
        self.tracer_provider = TracerProvider(resource=resource)
        self.tracer_provider.add_span_processor(processor)
        self.tracer = self.tracer_provider.get_tracer("alm")

        if metric_reader is None:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(),
                export_interval_millis=int(getattr(settings, "otel_metric_interval_seconds",
                                                   60) * 1000))
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        meter = self.meter_provider.get_meter("alm")

        self.runs = meter.create_counter(
            "alm_runs_total", description="Runs that reached an end or a pause, by outcome")
        self.writes = meter.create_counter(
            "alm_writes_total", description="Guarded writes, by operation and outcome")
        self.denials = meter.create_counter(
            "alm_policy_denials_total", description="Tool calls the policy refused")
        self.replays = meter.create_counter(
            "alm_replays_total", description="Writes the ledger turned into replays")
        self.tokens = meter.create_counter(
            "alm_model_tokens_total", unit="{token}",
            description="Model tokens, by model and direction (input/output)")
        self.model_latency = meter.create_histogram(
            "alm_model_latency_seconds", unit="s", description="Model call latency")
        self.approval_wait = meter.create_histogram(
            "alm_approval_wait_seconds", unit="s",
            description="From the approval card to the decision that resumed the run")
        self.jobs = meter.create_counter(
            "alm_jobs_total", description="Jobs a worker finished, by kind and result")

        # Gauges read values the worker refreshes; callbacks must not do I/O.
        self.queue_depth: dict[str, int] = {}
        self.busy: dict[str, int] = {}
        meter.create_observable_gauge(
            "alm_queue_depth", callbacks=[self._observe_queue],
            description="Jobs in the queue, by status")
        meter.create_observable_gauge(
            "alm_worker_busy", callbacks=[self._observe_busy],
            description="Jobs a worker is running now")

    # ------------------------------------------------------------- gauges

    def _observe_queue(self, _options):
        from opentelemetry.metrics import Observation

        return [Observation(n, {"status": s}) for s, n in sorted(self.queue_depth.items())]

    def _observe_busy(self, _options):
        from opentelemetry.metrics import Observation

        return [Observation(n, {"worker": w}) for w, n in sorted(self.busy.items())]

    # -------------------------------------------------------------- spans

    @contextmanager
    def run_span(self, thread_id: str, *, job_kind: str, attempt: int = 0, worker: str = ""):
        """The root span of one job on one run. Yields a :class:`RunSpans`, whose
        ``record`` method is a RunTrace listener."""
        from opentelemetry.trace import Status, StatusCode

        with self.tracer.start_as_current_span(
                f"run {job_kind}", attributes={"alm.thread_id": thread_id,
                                               "alm.job.kind": job_kind,
                                               "alm.job.attempt": attempt,
                                               "alm.worker": worker}) as root:
            spans = RunSpans(self, root)
            try:
                yield spans
            except BaseException as err:
                root.set_status(Status(StatusCode.ERROR, type(err).__name__))
                root.set_attribute("error.type", type(err).__name__)
                raise
            finally:
                spans.close()

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.meter_provider):
            try:
                provider.shutdown()
            except Exception:  # noqa: BLE001 - flushing at exit must not crash it
                log.exception("otel_shutdown_failed")


class RunSpans:
    """Turns one run's trace records into child spans, span events and metrics."""

    def __init__(self, telemetry: Telemetry, root):
        self.t = telemetry
        self.root = root
        self.agent = None
        self._lock = threading.Lock()

    def record(self, record: dict) -> None:
        """A RunTrace listener. Never raises."""
        try:
            with self._lock:
                self._record(record)
        except Exception:  # noqa: BLE001 - telemetry must not break a run
            log.exception("otel_record_failed")

    def close(self) -> None:
        with self._lock:
            self._end_agent()

    # ------------------------------------------------------------------

    def _record(self, record: dict) -> None:
        service, kind = record.get("service", ""), record.get("kind", "")
        if service == "supervisor":
            self._end_agent()
            target = str(record.get("next") or "")
            if target and target.upper() != "DONE":
                from opentelemetry import trace

                self.agent = self.t.tracer.start_span(
                    f"agent {target}", context=trace.set_span_in_context(self.root),
                    attributes={"alm.agent": target, "alm.hop": record.get("hop") or 0})
            self.root.add_event("supervisor", _attributes(record))
            return
        if service == "model" and kind == "call":
            self._model(record)
        elif service == "tool":
            self._span(f"tool {record.get('tool', '')}", record)
            if record.get("denied"):
                self.t.denials.add(1, {"tool": str(record.get("tool", "")),
                                       "agent": str(record.get("agent", ""))})
        elif service == "http":
            host = urlsplit(str(record.get("url", ""))).hostname or ""
            self._span(f"http {record.get('method', '')} {record.get('system') or host}",
                       record, extra={"http.request.method": str(record.get("method", "")),
                                      "server.address": host,
                                      "http.response.status_code": int(record.get("status")
                                                                       or 0)})
        elif service == "ledger":
            self._ledger(record)
        elif service in _SPAN_SERVICES and "ms" in record:
            self._span(f"{service} {kind}", record)
        elif service == "run" and kind == "finished":
            outcome = record.get("status", "")
            if record.get("halted") and outcome == "done":
                outcome = "halted"
            self.t.runs.add(1, {"outcome": str(outcome)})
            self.root.set_attribute("alm.outcome", str(outcome))
            self.root.add_event("finished", _attributes(record))
        elif service == "run" and kind == "parked":
            self.t.runs.add(1, {"outcome": "awaiting_approval"})
            self.root.add_event("parked")
        elif service in ("run", "approval", "agent") and kind not in ("agent_text", "log"):
            self.root.add_event(f"{service}.{kind}", _attributes(record))

    def _parent(self):
        from opentelemetry import trace

        return trace.set_span_in_context(self.agent or self.root)

    def _span(self, name: str, record: dict, *, extra: dict | None = None) -> None:
        from opentelemetry.trace import Status, StatusCode

        end = time.time_ns()
        start = end - int(record.get("ms") or 0) * 1_000_000
        attributes = {**_attributes(record), **(extra or {})}
        span = self.t.tracer.start_span(name, context=self._parent(), start_time=start,
                                        attributes=attributes)
        if record.get("ok") is False or record.get("denied"):
            span.set_status(Status(StatusCode.ERROR))
            error_type = _error_type(record.get("error"))
            if error_type:
                span.set_attribute("error.type", error_type)
        span.end(end_time=end)

    def _model(self, record: dict) -> None:
        model = str(record.get("model") or "")
        input_tokens = int(record.get("input_tokens") or 0)
        output_tokens = int(record.get("output_tokens") or 0)
        self._span(f"chat {model}", record, extra={
            "gen_ai.operation.name": "chat",
            "gen_ai.system": self.t.provider_name,
            "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "alm.caller": str(record.get("caller") or ""),
        })
        if input_tokens:
            self.t.tokens.add(input_tokens, {"model": model, "direction": "input"})
        if output_tokens:
            self.t.tokens.add(output_tokens, {"model": model, "direction": "output"})
        if record.get("ms") is not None:
            self.t.model_latency.record(int(record["ms"]) / 1000, {"model": model})

    def _ledger(self, record: dict) -> None:
        kind = record.get("kind", "")
        operation = str(record.get("operation", ""))
        (self.agent or self.root).add_event(f"ledger.{kind}", _attributes(record))
        if kind == "write":
            self.t.writes.add(1, {"operation": operation,
                                  "outcome": str(record.get("outcome", ""))})
        elif kind == "replay":
            self.t.replays.add(1, {"operation": operation})

    def _end_agent(self) -> None:
        if self.agent is not None:
            self.agent.end()
            self.agent = None


def _attributes(record: dict) -> dict:
    out = {}
    for key in _SAFE_FIELDS:
        value = record.get(key)
        if isinstance(value, bool | int | float) or (isinstance(value, str) and value):
            out[f"alm.{key}"] = value if not isinstance(value, str) else value[:120]
    return out


def _error_type(error) -> str:
    """``"TimeoutError: ..."`` -> ``"TimeoutError"``; the message is not exported."""
    if not error:
        return ""
    head = str(error).split(":", 1)[0].strip()
    return head if head.replace(".", "").replace("_", "").isalnum() and len(head) < 80 else ""


def _code_version() -> str:
    try:
        from importlib.metadata import version

        return version("alm-access-provisioning")
    except Exception:  # noqa: BLE001 - not installed as a package
        return "dev"
