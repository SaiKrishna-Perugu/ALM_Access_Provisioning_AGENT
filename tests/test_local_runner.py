"""The local runner: durable, scoped, redacted, and guarded like the CLI.

Everything here goes through the local wiring - ``get_store`` choosing SQLite,
``checkpointer_for`` choosing the SQLite checkpointer, the shared ``drive`` loop
- with the simulated estate standing in for EWM/JTS and a scripted model
standing in for Gemini. The real network steps are verified by
``python src/agent_local.py --check`` in the operator's environment.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("aiosqlite")
pytest.importorskip("langgraph.checkpoint.sqlite")

from langchain_core.messages import ToolMessage  # noqa: E402
from test_agentic_sandbox import AGENT_RE, ScriptedLLM, approve  # noqa: E402

from alm_agents import local  # noqa: E402
from alm_agents.runner import Console, drive, pending_interrupt  # noqa: E402
from alm_agents.sandbox import SandboxBackend, SandboxEstate  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.models import ApprovalDecision, RequestedUser, SourceWorkItem  # noqa: E402


class Paused(Exception):
    """Stands in for the operator walking away at the approval prompt."""


def walk_away(_payload):
    raise Paused


class RecordingLLM(ScriptedLLM):
    """Also keeps every message the model was shown."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen: list = []

    async def ainvoke(self, messages, **kw):
        self.seen.extend(messages)
        return await super().ainvoke(messages, **kw)


class Events(Console):
    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind, data):
        self.events.append((kind, data))


def local_settings(tmp_path, *, commit=True, **extra) -> Settings:
    base = Settings(_env_file=None, environment="TEST", orchestration="agentic",
                    llm_enabled=True, ledger_path=str(tmp_path / "local" / "alm.db"),
                    shadow_mode=not commit, **extra)
    return base


async def one_process(settings, llm, *, decide, thread_id, resume=False,
                      scope=("1001",), console=None, estate=None):
    """Everything local.run builds, minus the network: a fresh 'process'."""
    from alm_agents.agentic import AgenticRuntime, build_agentic_graph
    from alm_agents.graph import checkpointer_for
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_core.store import get_store
    from alm_core.tools.base import ToolContext

    store = await get_store(settings)
    ctx = ToolContext(settings=settings, client=None, store=store, run_id="")
    try:
        async with checkpointer_for(settings) as checkpointer:
            runtime = AgenticRuntime(
                ctx, llm=llm, memory=AgentMemory(None), max_hops=settings.max_hops,
                backend=SandboxBackend(estate or SandboxEstate.default()),
                on_event=console or Console(), shots_dir=str(settings.ledger_path) + "-shots")
            graph = build_agentic_graph(runtime, checkpointer=checkpointer)
            try:
                report = await drive(graph, ctx, thread_id=thread_id, decide=decide,
                                     console=console or Console(), resume=resume,
                                     work_item_ids=list(scope))
            except Paused:
                return {"paused": await pending_interrupt(graph, thread_id) is not None,
                        "writes": runtime.policy.writes_performed}
            report["policy"] = runtime.policy.summary()
            return report
    finally:
        await store.close()


PLAN_TO_APPROVAL = ["triage", "validator", "risk_officer"]
SCRIPTS_TO_APPROVAL = {
    "triage": [[("fetch_open_requests", {"limit": 10})]],
    "validator": [[("classify_user", {"userid": "AB12345"})]],
    "risk_officer": [[("request_human_approval",
                       {"reason": "one import", "userids": ["AB12345"]})]],
}
PROVISION = {"provisioner": [[("provision_jts_user", {"userid": "AB12345"})]]}


def _creates(report):
    return [r for r in report["results"]
            if r["operation"] == "jts_create" and r["userid"] == "AB12345"]


# ------------------------------------------------------------------ durability

def test_a_paused_run_resumes_in_a_new_process_and_writes_exactly_once(tmp_path):
    settings = local_settings(tmp_path)

    first = asyncio.run(one_process(
        settings, ScriptedLLM(PLAN_TO_APPROVAL, SCRIPTS_TO_APPROVAL),
        decide=walk_away, thread_id="local-t1"))
    assert first == {"paused": True, "writes": 0}

    # Everything rebuilt from the files alone, as after closing the terminal.
    second = asyncio.run(one_process(
        settings, ScriptedLLM(["provisioner", "DONE"], PROVISION),
        decide=approve, thread_id="local-t1", resume=True))
    assert not second["halted"], second["halt_reason"]
    assert [(r["outcome"], r["replayed"]) for r in _creates(second)] == [("ok", False)]


def test_a_later_run_on_the_same_work_item_replays_instead_of_repeating(tmp_path):
    settings = local_settings(tmp_path)
    full = ScriptedLLM(PLAN_TO_APPROVAL + ["provisioner", "DONE"],
                       {**SCRIPTS_TO_APPROVAL, **PROVISION})
    asyncio.run(one_process(settings, full, decide=approve, thread_id="local-a"))

    again = ScriptedLLM(PLAN_TO_APPROVAL + ["provisioner", "DONE"],
                        {**SCRIPTS_TO_APPROVAL, **PROVISION})
    report = asyncio.run(one_process(settings, again, decide=approve, thread_id="local-b"))
    assert [r["replayed"] for r in _creates(report)] == [True]


def test_without_commit_nothing_is_written_even_when_approved(tmp_path):
    settings = local_settings(tmp_path, commit=False)
    llm = ScriptedLLM(PLAN_TO_APPROVAL + ["provisioner", "DONE"],
                      {**SCRIPTS_TO_APPROVAL, **PROVISION})
    estate = SandboxEstate.default()
    report = asyncio.run(one_process(settings, llm, decide=approve, thread_id="dry",
                                     estate=estate))
    assert report["policy"]["writes"] == 0
    assert not estate.people["AB12345"].contributor


# ----------------------------------------------------------------------- scope

def test_a_scoped_run_cannot_read_or_write_outside_its_work_items(tmp_path):
    settings = local_settings(tmp_path)
    llm = ScriptedLLM(
        ["triage", "validator", "risk_officer", "provisioner", "closer", "DONE"], {
            "triage": [[("fetch_open_requests", {"limit": 10}),
                        ("fetch_work_item", {"work_item_id": "1002"})]],
            "validator": [[("classify_user", {"userid": "AB12345"}),
                           # Not on any in-scope work item: model-originated.
                           ("classify_user", {"userid": "GH22222"})]],
            "risk_officer": [[("request_human_approval",
                               {"reason": "x", "userids": ["AB12345", "GH22222"]})]],
            "provisioner": [[("provision_jts_user", {"userid": "GH22222"})]],
            "closer": [[("post_workitem_comment",
                         {"work_item_id": "1002", "text": "done", "userid": "EF11111"})]],
        })
    events = Events()
    estate = SandboxEstate.default()
    asyncio.run(one_process(settings, llm, decide=approve, thread_id="scoped",
                            console=events, estate=estate))

    calls = {d["tool"]: d["observation"] for k, d in events.events if k == "tool_call"}
    listed = json.loads(calls["fetch_open_requests"])
    assert [i["work_item_id"] for i in listed] == ["1001"]
    assert calls["fetch_work_item"].startswith("DENIED")
    assert calls["provision_jts_user"].startswith("DENIED")
    assert calls["post_workitem_comment"].startswith("DENIED")
    assert estate.comments["1002"] == []


# ------------------------------------------------------------------- redaction

def test_email_addresses_never_reach_the_model_but_stay_on_the_console(tmp_path):
    settings = local_settings(tmp_path, commit=False)
    llm = RecordingLLM(["triage", "DONE"],
                       {"triage": [[("fetch_work_item", {"work_item_id": "1001"})]]})
    events = Events()
    asyncio.run(one_process(settings, llm, decide=approve, thread_id="redact",
                            console=events))

    shown = [m.content for m in llm.seen if isinstance(m, ToolMessage)]
    assert shown and not any("example.com" in c for c in shown)
    assert any("AB12345" in c for c in shown)          # user IDs are kept
    console_view = [d["observation"] for k, d in events.events if k == "tool_call"]
    assert any("alice.smith@example.com" in o for o in console_view)


# ------------------------------------------------------------ settings guards

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                        # no stray .env
    for name in ("ALM_ENV", "ALM_CA_BUNDLE", "ALM_TLS_VERIFY", "EWM_SERVER", "JTS_SERVER"):
        monkeypatch.delenv(name, raising=False)

    def set_servers(ewm, jts):
        monkeypatch.setenv("EWM_SERVER", ewm)
        monkeypatch.setenv("JTS_SERVER", jts)
    return set_servers


TEST_EWM = "https://prssetst.intra.chrysler.com/ccm"
TEST_JTS = "https://prssetst.intra.chrysler.com/jts"
PROD_EWM = "https://prsse.intra.chrysler.com/ccm"
PROD_JTS = "https://prsse.intra.chrysler.com/jts"


def test_test_servers_give_a_dry_run_by_default(env, tmp_path):
    env(TEST_EWM, TEST_JTS)
    settings = local.build_settings(commit=False, ledger_path=str(tmp_path / "l.db"))
    assert settings.environment == "TEST" and settings.shadow_mode


def test_split_environments_are_refused(env):
    env(TEST_EWM, PROD_JTS)
    with pytest.raises(local.SetupError, match="EWM_SERVER is TEST but JTS_SERVER is PROD"):
        local.build_settings(commit=False)


def test_unknown_environment_is_refused(env):
    env("https://elm.example.net/ccm", "https://elm.example.net/jts")
    with pytest.raises(local.SetupError, match="ALM_ENV"):
        local.build_settings(commit=False)


def test_production_refuses_unverified_tls(env):
    env(PROD_EWM, PROD_JTS)
    with pytest.raises(local.SetupError, match="ALM_TLS_INSECURE"):
        local.build_settings(commit=True)


def test_production_with_a_ca_bundle_is_accepted(env, monkeypatch, tmp_path):
    env(PROD_EWM, PROD_JTS)
    bundle = tmp_path / "corp.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv("ALM_CA_BUNDLE", str(bundle))
    settings = local.build_settings(commit=True, ledger_path=str(tmp_path / "l.db"))
    assert settings.is_prod and not settings.shadow_mode
    assert settings.ca_bundle == str(bundle) and not settings.tls_insecure


def test_a_missing_ca_bundle_is_reported_plainly(env, monkeypatch):
    env(TEST_EWM, TEST_JTS)
    monkeypatch.setenv("ALM_CA_BUNDLE", "C:/nowhere/corp.pem")
    with pytest.raises(local.SetupError, match="does not exist"):
        local.build_settings(commit=False)


# ------------------------------------------------------------------------- GPT

class FakeGpt:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.attached = ""
        self.added: list = []
        FakeGpt.instances.append(self)

    def attach(self, cdp_url):
        self.attached = cdp_url

    def add_member(self, *, userid, group, domain):
        self.added.append((userid, group, domain))
        return True, "GPT accepted the request; AD provisioning is queued"

    def close(self):
        pass


def _gpt_ctx(tmp_path):
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    settings = local_settings(tmp_path)
    ctx = ToolContext(settings=settings, client=None, store=MemoryStore(), run_id="r")
    ctx.approval = ApprovalDecision(thread_id="t", approved=True, approver="test:me")
    user = RequestedUser(userid="AB12345",
                         source_work_items=[SourceWorkItem(work_item_id="1001")])
    return ctx, user


def test_the_ad_step_runs_in_the_attached_chrome_once_per_user(tmp_path, monkeypatch):
    import alm_worker.gpt as gpt

    FakeGpt.instances = []
    monkeypatch.setattr(gpt, "GptSession", FakeGpt)
    monkeypatch.setattr(local, "cdp_reachable", lambda _url, timeout=3.0: True)
    backend = local.make_local_backend(cdp_url="http://127.0.0.1:9222",
                                       gpt_url="https://gpt.example/home.jsf",
                                       ad_label="inetpsa.com")
    ctx, user = _gpt_ctx(tmp_path)

    async def scenario():
        first = await backend.request_group_membership(ctx, user, group="G", domain="D")
        second = await backend.request_group_membership(ctx, user, group="G", domain="D")
        return first, second

    try:
        first, second = asyncio.run(scenario())
    finally:
        backend.close()
    assert first.outcome.value == "ok" and "queued" in first.message
    assert second.replayed                                   # the ledger, not GPT
    session = FakeGpt.instances[0]
    assert session.attached == "http://127.0.0.1:9222"
    assert session.added == [("AB12345", "G", "D")]


def test_without_the_debug_chrome_the_ad_step_fails_with_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "cdp_reachable", lambda _url, timeout=3.0: False)
    backend = local.make_local_backend(cdp_url="http://127.0.0.1:9222",
                                       gpt_url="https://gpt.example/home.jsf",
                                       ad_label="inetpsa.com")
    ctx, user = _gpt_ctx(tmp_path)
    try:
        result = asyncio.run(
            backend.request_group_membership(ctx, user, group="G", domain="D"))
    finally:
        backend.close()
    assert result.outcome.value == "failed" and "start-gpt.ps1" in result.message


class DiesAtHop(ScriptedLLM):
    """Ctrl+C in the middle of the run: raised past every `except Exception`."""

    def __init__(self, die_at_supervisor_call: int, *a, **k):
        super().__init__(*a, **k)
        self.die_at = die_at_supervisor_call

    async def ainvoke(self, messages, **kw):
        is_supervisor = AGENT_RE.search(str(messages[0].content)) is None
        if is_supervisor and self.supervisor_calls + 1 == self.die_at:
            raise KeyboardInterrupt
        return await super().ainvoke(messages, **kw)


def test_a_run_killed_mid_way_resumes_from_its_last_checkpoint(tmp_path):
    settings = local_settings(tmp_path)
    dying = DiesAtHop(2, PLAN_TO_APPROVAL, SCRIPTS_TO_APPROVAL)   # dies choosing hop 2
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(one_process(settings, dying, decide=approve, thread_id="killed"))

    # Triage's work survived in the checkpoint: the resumed run starts at hop 2.
    events = Events()
    rest = ScriptedLLM(["validator", "risk_officer", "provisioner", "DONE"],
                       {**SCRIPTS_TO_APPROVAL, **PROVISION})
    report = asyncio.run(one_process(settings, rest, decide=approve, thread_id="killed",
                                     resume=True, console=events))
    hops = [d["hop"] for k, d in events.events if k == "supervisor"]
    assert hops[0] == 2
    assert [r["outcome"] for r in _creates(report)] == ["ok"]


# ----------------------------------------------------------------------- login

class FakeResponse:
    def __init__(self, url):
        self.url = url
        self.history = []


class FakeSession:
    def __init__(self, *, reachable=True, accept=True):
        self.reachable, self.accept = reachable, accept

    def get(self, url, **_kw):
        import requests

        if not self.reachable:
            raise requests.ConnectionError("name resolution failed")
        return FakeResponse(url)

    def post(self, url, **_kw):
        import requests

        if not self.reachable:
            raise requests.ConnectionError("name resolution failed")
        return FakeResponse(url if self.accept else url + "?authfailed=true")


def _login(session, verified=True):
    from alm_core.auth import form_login

    form_login(session, "https://host/ccm", "CID1", "pw", lambda _s: verified, timeout=1)


def test_an_unreachable_server_is_not_reported_as_bad_credentials():
    from alm_core.errors import TransportError

    with pytest.raises(TransportError, match="cannot reach .*NO_PROXY"):
        _login(FakeSession(reachable=False))


def test_rejected_credentials_say_so():
    from alm_core.errors import AuthenticationError

    with pytest.raises(AuthenticationError, match="rejected the user ID or password"):
        _login(FakeSession(accept=False))


def test_an_unverified_session_is_distinguished_from_a_rejection():
    from alm_core.errors import AuthenticationError

    with pytest.raises(AuthenticationError, match="did not verify"):
        _login(FakeSession(), verified=False)


class BrokenModel:
    """A key that the provider rejects: every call fails the same way."""

    def bind_tools(self, _tools, **_kw):
        return self

    async def ainvoke(self, _messages, **_kw):
        raise PermissionError("403 API key not valid")


def test_an_unusable_model_stops_the_run_once_instead_of_spending_every_hop(tmp_path):
    settings = local_settings(tmp_path, commit=False)
    report = asyncio.run(one_process(settings, BrokenModel(), decide=approve,
                                     thread_id="broken"))
    assert report["halted"] and "model is unavailable" in report["halt_reason"]
    assert report["hops"] == 1


def test_a_rejected_password_is_tried_only_once():
    from alm_core.errors import AuthenticationError

    session = FakeSession(accept=False)
    posts = []
    original = session.post
    session.post = lambda url, **kw: posts.append(url) or original(url, **kw)
    with pytest.raises(AuthenticationError):
        _login(session)
    assert len(posts) == 1


# ------------------------------------------------------------------ API keys

REAL_KEY = "AQ." + "Ab3-_" * 10


def test_a_windows_placeholder_does_not_shadow_the_key_in_env_file(tmp_path, monkeypatch):
    from alm_agents import sandbox

    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={REAL_KEY}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "{YOUR_API_KEY}")
    monkeypatch.setattr(sandbox, "KEY_ORIGIN", {})
    sandbox.load_env()
    assert sandbox.os.environ["GEMINI_API_KEY"] == REAL_KEY
    assert sandbox.KEY_ORIGIN["GEMINI_API_KEY"] == ".env"


def test_a_real_windows_key_still_wins_over_env_file(tmp_path, monkeypatch):
    from alm_agents import sandbox

    other = "AIza" + "B" * 35
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={REAL_KEY}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", other)
    monkeypatch.setattr(sandbox, "KEY_ORIGIN", {})
    sandbox.load_env()
    assert sandbox.os.environ["GEMINI_API_KEY"] == other
    assert sandbox.KEY_ORIGIN["GEMINI_API_KEY"] == "Windows environment"  # pragma: allowlist secret


def test_the_resolver_skips_a_placeholder_for_the_next_variable(monkeypatch):
    from alm_core.credentials import gemini_api_key

    monkeypatch.setenv("GEMINI_API_KEY", "<your-api-key>")
    monkeypatch.setenv("GOOGLE_API_KEY", REAL_KEY)
    assert gemini_api_key(object()) == REAL_KEY


def test_both_google_key_formats_are_redacted():
    from alm_core.logging import scrub_secrets

    classic = "AIza" + "C" * 35
    text = scrub_secrets(f"400 for key={classic} and {REAL_KEY}")
    assert classic not in text and REAL_KEY not in text


# ------------------------------------------------------- lessons from first run

def test_the_parser_reads_the_stored_field_not_the_models_redacted_copy(tmp_path):
    """First live run: the model passed back 'SOROBERTO,ANDREA,[email],SF58083;'
    and the parser rejected a perfectly good row because redaction removed '@'."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    ctx = ToolContext(settings=local_settings(tmp_path, commit=False), client=None,
                      store=MemoryStore(), run_id="t")
    board = Blackboard()
    registry = build_registry(ctx, board, AgentMemory(None),
                              backend=SandboxBackend(SandboxEstate.default()))

    async def scenario():
        await registry.get("fetch_work_item").run(work_item_id="1001")
        return json.loads(await registry.get("parse_new_users_field").run(
            work_item_id="1001", text="SMITH,ALICE,[email],AB12345;"))

    result = asyncio.run(scenario())
    assert result["rejected_rows"] == []
    assert [r["userid"] for r in result["parsed"]] == ["AB12345", "CD67890"]


def test_agents_are_told_that_redacted_emails_are_deliberate(tmp_path):
    settings = local_settings(tmp_path, commit=False)
    llm = RecordingLLM(["triage", "DONE"], {"triage": [[("finish", {"summary": "ok"})]]})
    asyncio.run(one_process(settings, llm, decide=approve, thread_id="note"))
    system = next(str(m.content) for m in llm.seen
                  if "You are the triage agent" in str(m.content))
    assert "[email]" in system and "on purpose" in system


# ------------------------------------------------- evidence and plain comments

EVIDENCE_COMMENT = "EF11111: User already present in JTS - no change needed"


def test_an_already_active_user_gets_evidence_and_an_unsigned_comment(tmp_path):
    llm = ScriptedLLM(
        ["triage", "validator", "risk_officer", "verifier", "evidence_officer",
         "closer", "DONE"], {
            "triage": [[("fetch_open_requests", {"limit": 10})]],
            "validator": [[("classify_user", {"userid": "EF11111"})]],
            "risk_officer": [[("request_human_approval",
                               {"reason": "already active; evidence and comment only",
                                "userids": ["EF11111"]})]],
            "verifier": [[("check_jazz_permission", {"userid": "EF11111"})]],
            "evidence_officer": [
                [("capture_evidence", {"userids": ["EF11111"]})],
                [("attach_workitem_evidence", {"work_item_id": "1002",
                                               "userid": "EF11111"})]],
            "closer": [[("post_workitem_comment",
                         {"work_item_id": "1002", "text": EVIDENCE_COMMENT,
                          "userid": "EF11111"})]],
        })
    estate = SandboxEstate.default()
    events = Events()
    report = asyncio.run(one_process(local_settings(tmp_path), llm, decide=approve,
                                     thread_id="evidence", scope=("1002",),
                                     estate=estate, console=events))
    assert not report["halted"], report["halt_reason"]
    assert estate.attachments["1002"] == ["EF11111.png"]
    # Posted exactly as written: no marker, no signature.
    assert estate.comments["1002"] == [EVIDENCE_COMMENT]
    captured = next(json.loads(d["observation"]) for k, d in events.events
                    if k == "tool_call" and d["tool"] == "capture_evidence")
    assert captured["captured"] == ["EF11111"] and captured["saved_in"]


def test_a_repeated_plain_comment_is_recognised_by_content():
    from alm_core.tools.ewm import normalize_comment

    posted = "EF11111: User already present in JTS - no change needed"
    stored = "EF11111: User already present in JTS&nbsp;- no change needed<br/>"
    assert normalize_comment(posted) in normalize_comment(stored.replace("&nbsp;", " "))
    assert normalize_comment("A &amp; B<br/>c") == "a & b c"


def test_evidence_uses_edge_on_windows_unless_configured(monkeypatch):
    import os

    from alm_core.tools import evidence

    monkeypatch.delenv("ALM_BROWSER_CHANNEL", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    assert evidence.browser_channel() == "msedge"
    monkeypatch.setattr(os, "name", "posix")
    assert evidence.browser_channel() == ""
    monkeypatch.setenv("ALM_BROWSER_CHANNEL", "chrome")
    assert evidence.browser_channel() == "chrome"
    monkeypatch.setenv("ALM_BROWSER_CHANNEL", "chromium")
    assert evidence.browser_channel() == ""
    monkeypatch.setenv("ALM_BROWSER_HEADED", "true")
    assert evidence.browser_headed()


# ------------------------------------------------------------ T9: run integrity

def test_a_paused_dry_run_cannot_be_resumed_as_a_commit(tmp_path):
    """Review finding: a dry run's preview approval became real on --resume --commit."""
    from alm_agents.runner import RunModeMismatch

    asyncio.run(one_process(
        local_settings(tmp_path, commit=False),
        ScriptedLLM(PLAN_TO_APPROVAL, SCRIPTS_TO_APPROVAL),
        decide=walk_away, thread_id="dry-1"))
    with pytest.raises(RunModeMismatch, match="dry-run"):
        asyncio.run(one_process(
            local_settings(tmp_path, commit=True),
            ScriptedLLM(["provisioner", "DONE"], PROVISION),
            decide=approve, thread_id="dry-1", resume=True))


def test_a_paused_commit_run_cannot_be_resumed_as_a_dry_run(tmp_path):
    """Review finding: --resume without --commit silently spent a real run's pause."""
    from alm_agents.runner import RunModeMismatch

    asyncio.run(one_process(
        local_settings(tmp_path, commit=True),
        ScriptedLLM(PLAN_TO_APPROVAL, SCRIPTS_TO_APPROVAL),
        decide=walk_away, thread_id="real-1"))
    with pytest.raises(RunModeMismatch, match="--resume real-1 --commit"):
        asyncio.run(one_process(
            local_settings(tmp_path, commit=False),
            ScriptedLLM(["provisioner", "DONE"], PROVISION),
            decide=approve, thread_id="real-1", resume=True))


def test_a_preview_approval_never_authorises_a_write():
    from alm_agents.policy import PolicyEngine
    from alm_core.errors import ApprovalRequired
    from alm_core.models import Operation
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext, guarded_write

    preview = ApprovalDecision(thread_id="t", approved=True,
                               approver="dry-run:auto-approve",
                               approved_userids=["AB12345"])
    policy = PolicyEngine(shadow=False, environment="TEST", approval=preview)
    verdict = policy.check("provision_jts_user", {"userid": "AB12345"})
    assert not verdict and "dry-run preview" in verdict.reason

    class Committing:
        shadow_mode = False
        environment = "TEST"
        max_concurrent_writes = 1

    ctx = ToolContext(settings=Committing(), client=None, store=MemoryStore(),
                      run_id="r", approval=preview)

    async def write():
        return await guarded_write(ctx, userid="AB12345", work_item_id="1001",
                                   operation=Operation.JTS_CREATE,
                                   action=lambda: pytest.fail("the write ran"))

    with pytest.raises(ApprovalRequired):
        asyncio.run(write())


def test_an_approval_covers_only_the_users_on_the_card(tmp_path):
    """Review finding: one 'y' covered users added after the human approved."""
    seen = []

    def record(payload):
        from alm_agents.runner import ask_for_decision

        seen.append(sorted(i["userid"] for i in payload["items"]))
        # The real decision path: what the operator gets after typing "y".
        return ask_for_decision(payload, auto=True, console=Events(),
                                approver_prefix="test")

    llm = ScriptedLLM(
        ["triage", "validator", "risk_officer", "validator", "provisioner", "DONE"], {
            "triage": [[("fetch_open_requests", {"limit": 10})]],
            "validator": [[("classify_user", {"userid": "AB12345"})],
                          [("finish", {"summary": "ok"})],
                          # After the approval: a user nobody has seen yet.
                          [("classify_user", {"userid": "TB22322"})]],
            "risk_officer": [[("request_human_approval",
                               {"reason": "one import", "userids": ["AB12345"]})]],
            "provisioner": [[("provision_jts_user", {"userid": "AB12345"})]],
        })
    report = asyncio.run(one_process(local_settings(tmp_path), llm, decide=record,
                                     thread_id="cover"))
    assert report["approval_rounds"] == 2
    assert "TB22322" not in seen[0] and "TB22322" in seen[1]


def test_the_decision_names_the_users_it_covers():
    from alm_agents.runner import ask_for_decision

    decision = ask_for_decision(
        {"thread_id": "t", "plan_hash": "h",
         "items": [{"userid": "AB12345", "risk": "low", "state": "ready"},
                   {"userid": "CD67890", "risk": "low", "state": "archived"}]},
        auto=True, console=Events(), approver_prefix="local")
    assert decision.approved_userids == ["AB12345", "CD67890"]
    assert not decision.covers("EF11111")


def test_a_resumed_run_keeps_its_prod_confirmation_and_budgets(tmp_path):
    from alm_agents.agentic import AgenticRuntime
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    ctx = ToolContext(settings=local_settings(tmp_path), client=None,
                      store=MemoryStore(), run_id="")
    runtime = AgenticRuntime(ctx, llm=None, memory=AgentMemory(None),
                             backend=SandboxBackend(SandboxEstate.default()))
    runtime.sync_from({"policy": {"prod_confirmed": True, "writes": 7, "tool_calls": 40}})
    assert runtime.policy.prod_confirmed
    assert (runtime.policy.writes_performed, runtime.policy.tool_calls) == (7, 40)
