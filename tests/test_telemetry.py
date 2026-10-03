"""OpenTelemetry: a worker run becomes a span tree and metrics, with nothing
private in what is exported."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("opentelemetry.sdk")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

from langchain_core.messages import AIMessage  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from test_agentic_sandbox import ScriptedLLM  # noqa: E402
from test_worker import PLAN, run_with, scripts, start  # noqa: E402

from alm_agents.trace import TracedModel  # noqa: E402
from alm_core import telemetry  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.models import ApprovalDecision  # noqa: E402

PRIVATE = ("AB12345", "alice", "Alice", "@example.com", "calibration", "import AB12345")


class Counted(ScriptedLLM):
    async def ainvoke(self, messages, **kw):
        response = await super().ainvoke(messages, **kw)
        return AIMessage(content=response.content, tool_calls=response.tool_calls,
                         usage_metadata={"input_tokens": 120, "output_tokens": 30,
                                         "total_tokens": 150})


@pytest.fixture
def otel():
    spans, metrics = InMemorySpanExporter(), InMemoryMetricReader()
    telemetry.shutdown()
    t = telemetry.setup(Settings(_env_file=None, environment="TEST"), service="alm-test",
                        span_exporter=spans, metric_reader=metrics)
    yield t, spans, metrics
    telemetry.shutdown()


def _metric_points(reader) -> dict[str, list]:
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = [(dict(p.attributes), getattr(p, "value", None)
                                     if hasattr(p, "value") else p.count)
                                    for p in metric.data.data_points]
    return out


def test_a_run_is_a_tree_of_spans_with_nothing_private(tmp_path, otel):
    _t, spans, metrics = otel

    async def scenario(h):
        # As in production (llm._get): the model client records every call.
        h.services.agent_llm = TracedModel(h.router, role="agent")
        h.services.supervisor_llm = TracedModel(h.router, role="supervisor")
        await start(h)
        await h.worker("w1").drain()
        request, _ = await h.store.get_approval("wi-1001")
        from alm_agents.worker import submit_decision

        await submit_decision(h.store, "wi-1001", ApprovalDecision(
            thread_id="wi-1001", approved=True, approver="web:boss",
            plan_hash=request.plan_hash, approved_userids=["AB12345"]))
        await h.worker("w2").drain()
        return await h.store.get_run("wi-1001")

    run = run_with(tmp_path, scenario, make=lambda _t: Counted(PLAN, scripts()))
    assert run["status"] == "done", run["error"]
    finished = spans.get_finished_spans()
    by_id = {s.context.span_id: s for s in finished}
    roots = [s for s in finished if s.parent is None]
    assert sorted(s.name for s in roots) == ["run resume", "run start"]
    assert all(r.attributes["alm.thread_id"] == "wi-1001" for r in roots)

    agents = [s for s in finished if s.name.startswith("agent ")]
    assert {"agent triage", "agent validator", "agent risk_officer",
            "agent provisioner"} <= {s.name for s in agents}
    assert all(by_id[s.parent.span_id].name.startswith("run ") for s in agents)

    tools = {s.name: s for s in finished if s.name.startswith("tool ")}
    assert by_id[tools["tool provision_jts_user"].parent.span_id].name == "agent provisioner"
    assert tools["tool provision_jts_user"].attributes["alm.write"] is True

    chats = [s for s in finished if s.attributes.get("gen_ai.operation.name") == "chat"]
    assert chats and all(s.attributes["gen_ai.usage.input_tokens"] == 120 for s in chats)

    events = [e.name for r in roots for e in r.events]
    assert "ledger.write" in [e.name for s in agents for e in s.events]
    assert "parked" in events and "finished" in events
    resumed = next(r for r in roots if r.name == "run resume")
    assert resumed.attributes["alm.outcome"] == "done"

    exported = json.dumps([{"name": s.name, "attributes": dict(s.attributes),
                            "events": [[e.name, dict(e.attributes)] for e in s.events]}
                           for s in finished], default=str)
    leaked = [p for p in PRIVATE if p in exported]
    assert not leaked, leaked

    points = _metric_points(metrics)
    assert ({"outcome": "awaiting_approval"}, 1) in points["alm_runs_total"]
    assert ({"outcome": "done"}, 1) in points["alm_runs_total"]
    assert ({"operation": "jts_create", "outcome": "ok"}, 1) in points["alm_writes_total"]
    tokens = {tuple(sorted(a.items())): v for a, v in points["alm_model_tokens_total"]}
    assert sum(tokens.values()) == 150 * len(chats)
    assert points["alm_approval_wait_seconds"][0][1] == 1      # one decision observed
    assert {a["kind"] for a, _v in points["alm_jobs_total"]} == {"start", "resume"}


def test_telemetry_is_off_unless_enabled():
    telemetry.shutdown()
    assert telemetry.setup(Settings(_env_file=None), service="alm-test") is None
    assert telemetry.get() is None


def test_only_allowlisted_fields_and_error_types_are_exported():
    record = {"service": "tool", "kind": "tool_call", "tool": "classify_user", "ok": False,
              "args": {"userid": "AB12345"}, "observation": "Alice Smith",
              "error": "TimeoutError: AB12345 did not answer", "denied": True}
    assert telemetry._attributes(record) == {
        "alm.service": "tool", "alm.kind": "tool_call", "alm.tool": "classify_user",
        "alm.ok": False, "alm.denied": True}
    assert telemetry._error_type(record["error"]) == "TimeoutError"
    assert telemetry._error_type("the user AB12345 is unknown") == ""


def test_the_dashboards_and_alerts_name_only_metrics_the_code_exports():
    """A renamed metric must not leave a dashboard or an alert silently empty."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "src" / "alm_core" / "telemetry.py").read_text(encoding="utf-8")
    exported = set(re.findall(r'"(alm_[a-z_]+)"', source))
    texts = [(root / "ops" / "alerts.md").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in (root / "ops" / "dashboards").glob("*.json")]
    named = {re.sub(r"_(bucket|count|sum)$", "", m)
             for text in texts for m in re.findall(r"\b(alm_[a-z_]+)\b", text)}
    named -= {"alm_core", "alm_agents"}      # module paths in prose
    assert named and named <= exported, named - exported
