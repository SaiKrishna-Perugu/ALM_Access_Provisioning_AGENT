"""AI governance: token budgets, the provider allowlist, withheld fields, the
version every run records, and the fixed-order fallback when the model is down.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

from langchain_core.messages import AIMessage  # noqa: E402
from test_agentic_sandbox import ScriptedLLM  # noqa: E402
from test_worker import PLAN, scripts, start  # noqa: E402
from test_worker import run_with as worker_run_with  # noqa: E402

from alm_agents.agent import withhold_fields  # noqa: E402
from alm_agents.graph import run_session  # noqa: E402
from alm_agents.policy import PolicyEngine  # noqa: E402
from alm_agents.version import orchestration_of, prompt_version, run_version  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.errors import ConfigError  # noqa: E402

VERSION_RE = re.compile(r"^agentic-[0-9a-f]{12}$")


class CountedLLM(ScriptedLLM):
    """A scripted model that reports token usage, as real providers do."""

    def __init__(self, *args, tokens_per_call: int = 1000, **kw):
        super().__init__(*args, **kw)
        self.tokens_per_call = tokens_per_call

    async def ainvoke(self, messages, **kw):
        response = await super().ainvoke(messages, **kw)
        half = self.tokens_per_call // 2
        return AIMessage(content=response.content, tool_calls=response.tool_calls,
                         usage_metadata={"input_tokens": half, "output_tokens": half,
                                         "total_tokens": 2 * half})


class DownLLM:
    """A model whose every call fails, like a provider outage."""

    def bind_tools(self, _tools, **_kw):
        return self

    async def ainvoke(self, _messages, **_kw):
        raise RuntimeError("503 the model is overloaded")


def run_with(tmp_path, scenario, *, settings_update=None, **kw):
    """test_worker.run_with with extra settings for the run."""
    if settings_update:
        import test_worker

        original = test_worker.local_settings

        def patched(*a, **k):
            return original(*a, **k).model_copy(update=settings_update)

        test_worker.local_settings = patched
        try:
            return worker_run_with(tmp_path, scenario, **kw)
        finally:
            test_worker.local_settings = original
    return worker_run_with(tmp_path, scenario, **kw)


# ------------------------------------------------------------ token budgets

def test_the_policy_counts_tokens_and_knows_when_the_budget_is_spent():
    policy = PolicyEngine(environment="TEST", shadow=True, max_tokens=1500)
    policy.note_tokens(AIMessage(content="", usage_metadata={
        "input_tokens": 600, "output_tokens": 400, "total_tokens": 1000}))
    assert policy.tokens == 1000 and not policy.tokens_exhausted
    policy.note_tokens(AIMessage(content=""))           # a provider that reports nothing
    policy.note_tokens(AIMessage(content="", usage_metadata={
        "input_tokens": 500, "output_tokens": 0, "total_tokens": 500}))
    assert policy.tokens_exhausted and policy.summary()["tokens"] == 1500
    assert not PolicyEngine(environment="TEST", shadow=True).tokens_exhausted  # 0 = no cap


def test_a_run_halts_when_it_has_spent_its_token_budget(tmp_path):
    async def scenario(h):
        await start(h, mode="dry")
        await h.worker("w").drain()
        return await h.store.get_run("wi-1001")

    run = run_with(tmp_path, scenario, settings_update={"max_tokens_per_run": 2500},
                   make=lambda _t: CountedLLM(PLAN, scripts(), tokens_per_call=1000))
    report = run["report"]
    assert report["halted"] and "token budget" in report["halt_reason"]
    assert 2500 <= report["metrics"]["tokens"] < 5000
    assert report["results"] == []


def test_the_daily_cap_refuses_a_new_run_once_spent(tmp_path):
    async def scenario(h):
        await h.store.upsert_run("earlier", status="done", report={
            "metrics": {"tokens": 9000}})
        await start(h, mode="dry")
        await h.worker("w").drain()
        return await h.store.get_run("wi-1001"), await h.store.tokens_since(
            datetime(2000, 1, 1, tzinfo=timezone.utc))

    run, spent = run_with(tmp_path, scenario, settings_update={"max_tokens_per_day": 5000})
    assert spent == 9000
    assert run["status"] == "failed" and "daily model token budget" in run["error"]
    assert run["report"] is None


def test_the_daily_cap_degrades_to_the_fixed_order_when_allowed(tmp_path):
    async def scenario(h):
        await h.store.upsert_run("earlier", status="done", report={
            "metrics": {"tokens": 9000}})
        worker = h.worker("w")

        class Trace:
            records = []

            def write(self, record):
                self.records.append(record)

        under = await worker._within_daily_budget("wi-1001", Trace())
        return under, Trace.records

    under, records = run_with(tmp_path, scenario, settings_update={
        "max_tokens_per_day": 5000, "degrade_on_model_failure": True})
    assert under == "deterministic"
    assert records == [{"service": "run", "kind": "token_cap", "used": 9000, "cap": 5000}]


# ---------------------------------------------------------- model down

def test_a_model_outage_queues_a_fixed_order_rerun_when_allowed(tmp_path):
    async def scenario(h):
        await start(h, mode="dry")
        worker = h.worker("w")
        # Only the first run: the fallback needs the live estate, not the sandbox.
        job = await h.store.claim_job(worker.worker_id, 60, kinds=worker_kinds())
        await worker.handle(job)
        return (await h.store.get_run("wi-1001"), await h.store.get_run("wi-1001-fallback"),
                await h.store.list_jobs(status="queued"))

    first, fallback, queued = run_with(tmp_path, scenario, make=lambda _t: DownLLM(),
                                       settings_update={"degrade_on_model_failure": True})
    assert first["report"]["halted"]
    assert first["report"]["halt_reason"].startswith("the agent model is unavailable")
    assert fallback["trigger"] == "fallback" and fallback["mode"] == "dry"
    assert fallback["scope"] == ["1001"] and fallback["requested_by"] == "test"
    assert [(j["thread_id"], j["payload"]["orchestration"]) for j in queued] == [
        ("wi-1001-fallback", "deterministic")]


def test_a_model_outage_queues_nothing_by_default(tmp_path):
    async def scenario(h):
        await start(h, mode="dry")
        await h.worker("w").drain()
        return await h.store.get_run("wi-1001-fallback"), await h.store.list_jobs(
            status="queued")

    fallback, queued = run_with(tmp_path, scenario, make=lambda _t: DownLLM())
    assert fallback is None and queued == []


def worker_kinds():
    from alm_agents.worker import RUN_KINDS

    return RUN_KINDS


# --------------------------------------------------------------- version

def test_every_run_records_the_version_of_what_decided_it(tmp_path):
    from alm_core.models import ApprovalDecision

    async def scenario(h):
        await start(h)
        await h.worker("w1").drain()
        parked = await h.store.get_run("wi-1001")
        request, _ = await h.store.get_approval("wi-1001")
        from alm_agents.worker import submit_decision

        await submit_decision(h.store, "wi-1001", ApprovalDecision(
            thread_id="wi-1001", approved=True, approver="web:boss",
            plan_hash=request.plan_hash, approved_userids=["AB12345"]))
        await h.worker("w2").drain()
        return parked, await h.store.get_run("wi-1001")

    parked, done = run_with(tmp_path, scenario)
    assert VERSION_RE.match(parked["version"]), parked["version"]
    assert done["version"] == parked["version"] == done["report"]["version"]


def test_the_version_changes_with_the_prompts_and_names_the_orchestration(monkeypatch):
    settings = Settings(_env_file=None, orchestration="guided")
    before = prompt_version(settings)
    assert prompt_version(settings) == before                       # stable
    assert prompt_version(settings.model_copy(update={"agent_model": "other"})) != before
    from alm_agents import supervisor

    monkeypatch.setattr(supervisor, "SUPERVISOR_PROMPT", supervisor.SUPERVISOR_PROMPT + "!")
    assert prompt_version(settings) != before
    assert run_version(settings).startswith("guided-")
    assert run_version(settings, "deterministic").startswith("deterministic-")
    assert orchestration_of("deterministic-abc") == "deterministic"
    assert orchestration_of("") == orchestration_of("v1.2") == ""


def test_a_run_can_be_pinned_to_an_orchestration():
    from alm_agents.graph import Services

    settings = Settings(_env_file=None, orchestration="agentic", environment="TEST")
    services = Services(settings=settings, store=None, client=None, checkpointer=None)
    _graph, ctx = run_session(services, orchestration="deterministic")
    assert ctx.settings.orchestration == "deterministic"
    with pytest.raises(ConfigError, match="no model client"):
        run_session(Services(settings=settings.model_copy(
            update={"orchestration": "deterministic"}), store=None, client=None,
            checkpointer=None), orchestration="agentic")


# ------------------------------------------------- what reaches the model

def test_withheld_fields_never_reach_the_model_at_any_depth():
    observation = json.dumps({"work_item": {"id": "1001", "justification": "secret plans",
                                            "users": [{"userid": "AB12345",
                                                       "summary": "x" * 7}]}})
    seen = json.loads(withhold_fields(observation, frozenset({"justification", "summary"})))
    assert seen["work_item"]["justification"] == "[withheld: 12 chars]"
    assert seen["work_item"]["users"][0] == {"userid": "AB12345",
                                             "summary": "[withheld: 7 chars]"}
    assert withhold_fields("plain text", frozenset({"summary"})) == "plain text"
    assert withhold_fields(observation, frozenset()) == observation


def test_a_provider_outside_the_allowlist_is_refused_at_start_up():
    with pytest.raises(ValueError, match="ALM_ALLOWED_PROVIDERS"):
        Settings(_env_file=None, llm_enabled=True, llm_provider="gemini_api",
                 allowed_providers="vertex,bedrock")
    ok = Settings(_env_file=None, llm_enabled=True, llm_provider="gemini_api",
                  allowed_providers="vertex, gemini_api")
    assert ok.llm_provider == "gemini_api"


def test_a_withheld_field_never_reaches_the_model_in_a_real_run(tmp_path):
    """The whole path: a tool returns the field, redaction runs, the model reads."""
    import asyncio

    from test_agentic_sandbox import sandbox_settings

    from alm_agents.runner import Console
    from alm_agents.sandbox import SandboxEstate, run_sandbox

    class Reading(ScriptedLLM):
        seen: list[str] = []

        async def ainvoke(self, messages, **kw):
            self.seen.extend(str(m.content) for m in messages)
            return await super().ainvoke(messages, **kw)

    events = []

    class Recording(Console):
        def __call__(self, kind, data):
            events.append((kind, data))
            return super().__call__(kind, data)

    llm = Reading(["triage", "DONE"], {"triage": [
        [("fetch_work_item", {"work_item_id": "1001"})]]})
    asyncio.run(run_sandbox(
        sandbox_settings(model_withheld_fields="justification"), llm=llm, supervisor_llm=llm,
        console=Recording(), estate=SandboxEstate.default(), shots_dir=str(tmp_path)))
    seen = "\n".join(Reading.seen)
    assert "[withheld:" in seen
    assert "calibration programme" not in seen
    traced = [d for k, d in events if k == "tool_call" and d.get("tool") == "fetch_work_item"]
    assert traced and traced[0]["withheld"] == ["justification"]
