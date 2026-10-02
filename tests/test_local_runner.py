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


def local_settings(tmp_path, *, commit=True, orchestration="agentic", **extra) -> Settings:
    base = Settings(_env_file=None, environment="TEST", orchestration=orchestration,
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


TEST_EWM = "https://prssetst.example.intra/ccm"
TEST_JTS = "https://prssetst.example.intra/jts"
PROD_EWM = "https://prsse.example.intra/ccm"
PROD_JTS = "https://prsse.example.intra/jts"


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
    with pytest.raises(local.SetupError, match=r"unverified TLS \(PROD\)"):
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

    # What GPT shows after Modify; a test may swap it, or make clicking raise.
    reply = "Your request has been submitted correctly. Failed Requests: 0"
    click_raises = False
    closed = 0

    def open_group(self, group):
        self.group = group

    def stage_user(self, userid, domain):
        self.staged = (userid, domain)
        return True

    def click_modify(self):
        userid, domain = self.staged
        self.added.append((userid, self.group, domain))
        if FakeGpt.click_raises:
            raise RuntimeError("tab crashed")
        return FakeGpt.reply

    def close(self):
        FakeGpt.closed += 1


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


def test_an_untrusted_certificate_points_at_the_ca_bundle_not_the_vpn():
    import requests

    from alm_core.errors import TransportError

    class BadCertificate(FakeSession):
        def post(self, url, **_kw):
            raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    with pytest.raises(TransportError, match="not trusted.*ALM_CA_BUNDLE") as caught:
        _login(BadCertificate())
    assert "NO_PROXY" not in caught.value.message


@pytest.mark.parametrize(("error", "proxy", "expected"), [
    ("SSLError", "", "REQUESTS_CA_BUNDLE and SSL_CERT_FILE"),
    ("ProxyError", "http://proxy:8080", "proxy in HTTPS_PROXY refused"),
    ("ConnectTimeout", "", "Set HTTPS_PROXY"),
    ("ConnectionError", "", "Set HTTPS_PROXY"),
    ("ConnectionError", "http://proxy:8080", "through HTTPS_PROXY"),
])
def test_gemini_network_failures_get_the_fix_that_matches(monkeypatch, error, proxy,
                                                           expected):
    """Review DX #8: behind TLS inspection 'set HTTPS_PROXY' was the wrong advice."""
    import requests

    from alm_agents.sandbox import network_advice

    monkeypatch.delenv("https_proxy", raising=False)
    if proxy:
        monkeypatch.setenv("HTTPS_PROXY", proxy)
    else:
        monkeypatch.delenv("HTTPS_PROXY", raising=False)
    advice = network_advice(getattr(requests.exceptions, error)("boom"))
    assert expected in advice
    if error == "SSLError":
        assert "HTTPS_PROXY" not in advice


def test_check_warns_but_passes_without_the_gpt_chrome(tmp_path, monkeypatch):
    """Review DX #9: an optional step failed --check outright."""
    import alm_agents.local as local
    import alm_agents.sandbox as sandbox
    import alm_core.auth as auth
    import alm_core.credentials as credentials
    from alm_core.tools import ewm

    class Client:
        def __init__(self, *_a):
            pass

        def session(self, *_a, **_k):
            return None

        def close(self):
            pass

    async def no_requests(*_a):
        return []

    monkeypatch.setattr(sandbox, "check", lambda *_a: 0)
    monkeypatch.setattr(credentials, "build_resolver", lambda *_a, **_k: None)
    monkeypatch.setattr(auth, "JazzClient", Client)
    monkeypatch.setattr(ewm, "fetch_open_requests", no_requests)
    monkeypatch.setattr(local, "cdp_reachable", lambda _url: False)
    monkeypatch.setattr(local, "_launch_browser", lambda _channel: None)

    settings = local_settings(tmp_path, ewm_server="https://host/ccm",
                              jts_server="https://host/jts", CID="CID1")
    console = Events()
    lines: list[str] = []
    console.line = lines.append
    code = asyncio.run(local.check(settings, console, []))
    assert code == 0, lines
    assert any(line.startswith("WARN  GPT Chrome") for line in lines)
    assert not any(line.startswith("FAIL") for line in lines)
    assert any("1 warning(s)" in line for line in lines)


def test_the_gpt_target_ignores_blank_template_values(monkeypatch):
    """.env.example ships CDP_URL= / GPT_URL= / AD_LABEL=; blank means default."""
    from alm_agents.local import DEFAULT_GPT_URL, gpt_target

    for key in ("CDP_URL", "GPT_URL", "AD_LABEL"):
        monkeypatch.setenv(key, "")
    target = gpt_target()
    assert target["gpt_url"] == DEFAULT_GPT_URL
    assert target["cdp_url"] == "http://127.0.0.1:9222"
    assert target["ad_label"]


def test_the_clis_workitem_spelling_works_too():
    from alm_agents.local import parse_args

    assert parse_args(["--workitem", "123", "--work-item", "456"]).work_item == ["123", "456"]


def test_the_password_prompt_names_the_account_not_the_secret(tmp_path, monkeypatch):
    import alm_core.credentials as credentials
    from alm_agents.local import jazz_password_resolver

    asked: list[str] = []
    monkeypatch.delenv("EWM_PASSWORD", raising=False)
    monkeypatch.setattr(credentials, "_has_console", lambda: True)
    monkeypatch.setattr(credentials.getpass, "getpass",
                        lambda question: asked.append(question) or "pw")
    settings = local_settings(tmp_path, CID="CID1")
    resolver = jazz_password_resolver(settings)
    assert resolver.get(settings.password_secret_name) == "pw"  # pragma: allowlist secret
    assert asked == ["Jazz password for CID1: "]


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
    """First live run: the model passed back 'DOE,JANE,[email],AB12345;'
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


def test_a_user_is_classified_once_until_something_is_written_for_them(tmp_path):
    """Review P2: the validator, risk officer and remediator each re-read LDAP/JTS."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    class CountingBackend(SandboxBackend):
        lookups = 0

        async def classify_user(self, ctx, user):
            CountingBackend.lookups += 1
            return await super().classify_user(ctx, user)

    ctx = ToolContext(settings=local_settings(tmp_path, commit=False), client=None,
                      store=MemoryStore(), run_id="t")
    registry = build_registry(ctx, Blackboard(), AgentMemory(None),
                              backend=CountingBackend(SandboxEstate.default()))
    classify = registry.get("classify_user")

    async def scenario():
        await registry.get("fetch_work_item").run(work_item_id="1002")
        first = json.loads(await classify.run(userid="EF11111"))
        second = json.loads(await classify.run(userid="EF11111"))
        after_first_two = CountingBackend.lookups
        await registry.get("provision_jts_user").run(userid="EF11111")
        third = json.loads(await classify.run(userid="EF11111"))
        return first, second, third, after_first_two

    first, second, third, after_first_two = asyncio.run(scenario())
    assert after_first_two == 1
    assert "note" not in first and "still current" in second["note"]
    assert second["registry_state"] == first["registry_state"]
    assert CountingBackend.lookups == 2 and "note" not in third


def test_agent_memory_outlives_the_process_in_the_local_ledger_file(tmp_path):
    """Review E1: a local run forgot everything the previous run learned."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_core.store.sqlite import SqliteStore

    path = str(tmp_path / "alm.db")

    async def first_run():
        store = SqliteStore(path)
        await store.start()
        await store.migrate()
        memory = AgentMemory(store)
        await memory.migrate()
        assert memory.durable
        await memory.remember(kind="semantic", content="template X puts users in "
                              "the Justification field", subject="template-x",
                              tags=["Parsing"], author="agent", confidence=0.7)
        await memory.remember(kind="episodic", content="GH22222 not in LDAP",
                              subject="GH22222", author="agent")
        await store.close()

    async def second_run():
        store = SqliteStore(path)
        await store.start()
        memory = AgentMemory(store)
        await memory.migrate()
        hints = await memory.recall(tags=["parsing"], kind="semantic")
        brief = await memory.brief(["GH22222"], tags=["parsing"])
        retired = await memory.supersede("GH22222")
        after = await memory.recall(subject="GH22222")
        await store.close()
        return hints, brief, retired, after

    asyncio.run(first_run())
    hints, brief, retired, after = asyncio.run(second_run())
    assert [h["subject"] for h in hints] == ["template-x"]
    assert hints[0]["tags"] == ["parsing"]
    assert "GH22222 not in LDAP" in brief and "Justification" in brief
    assert retired == 1 and after == []


def test_purge_removes_old_personal_data_and_keeps_the_ledger(tmp_path):
    """Review S7: local runs kept requester names and e-mails indefinitely."""
    import os
    from datetime import datetime, timedelta, timezone

    from alm_agents.local import parse_args, purge
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_core.store.sqlite import SqliteStore

    settings = local_settings(tmp_path)
    llm = ScriptedLLM(PLAN_TO_APPROVAL, SCRIPTS_TO_APPROVAL)
    assert asyncio.run(one_process(settings, llm, decide=walk_away,
                                   thread_id="old"))["paused"]

    out = tmp_path / "out"
    (out / "evidence" / "old").mkdir(parents=True)
    (out / "evidence" / "old" / "AB12345.png").write_bytes(b"png")
    (out / "run-old.json").write_text("{}", encoding="utf-8")
    (out / "traces").mkdir()
    (out / "traces" / "old.jsonl").write_text('{"service": "run"}', encoding="utf-8")

    async def audit_rows_and_remember():
        store = SqliteStore(settings.ledger_path)
        await store.start()
        memory = AgentMemory(store)
        await memory.migrate()
        await memory.remember(kind="episodic", content="Alice Smith was imported",
                              subject="AB12345", author="agent")
        rows = await store._fetchall("SELECT COUNT(*) FROM alm_audit")
        await store.close()
        return rows[0][0]

    audit_before = asyncio.run(audit_rows_and_remember())
    assert audit_before > 0

    fresh = asyncio.run(purge(settings, Events(), 30, out_dir=out))
    assert set(fresh.values()) == {0}  # nothing is old yet

    later = datetime.now(timezone.utc) + timedelta(days=31)
    old = (later - timedelta(days=40)).timestamp()
    for path in [out / "run-old.json", out / "evidence" / "old" / "AB12345.png",
                 out / "traces" / "old.jsonl"]:
        os.utime(path, (old, old))
    preview = asyncio.run(purge(settings, Events(), 30, out_dir=out, now=later,
                                dry_run=True))
    assert (out / "run-old.json").exists() and (out / "evidence" / "old").exists()
    purged = asyncio.run(purge(settings, Events(), 30, out_dir=out, now=later))
    assert purged == preview == {
        "runs": 1, "approvals": 1, "memories": 1, "reports": 1, "evidence": 1,
        "cli_screenshots": 0, "cli_users": 0, "cli_comments": 0, "recordings": 0,
        "cli_backups": 0, "cli_state": 0, "test_runs": 0, "traces": 1,
    }
    assert not (out / "run-old.json").exists()
    assert not (out / "traces" / "old.jsonl").exists()
    assert not (out / "evidence" / "old").exists()

    async def audit_rows():
        store = SqliteStore(settings.ledger_path)
        await store.start()
        rows = await store._fetchall("SELECT COUNT(*) FROM alm_audit")
        await store.close()
        return rows[0][0]

    assert asyncio.run(audit_rows()) == audit_before
    with pytest.raises(SystemExit):
        parse_args(["--purge-older-than", "0"])
    with pytest.raises(SystemExit):
        parse_args(["--dry-run"])  # only meaningful with --purge-older-than
    assert parse_args(["--purge-older-than", "7", "--dry-run"]).dry_run


def test_a_cut_observation_says_how_much_was_cut():
    from alm_agents.agent import clip_observation

    assert clip_observation("short", limit=100) == "short"
    clipped = clip_observation("x" * 10_000, limit=6000)
    assert 5990 <= len(clipped) <= 6000
    omitted = int(clipped.rsplit("[", 1)[1].split()[0])
    assert clipped.count("x") + omitted == 10_000


def test_an_empty_queue_is_reported_as_empty(tmp_path):
    """Review Q5: json.dumps([]) is truthy, so the fallback text never appeared."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    ctx = ToolContext(settings=local_settings(tmp_path, commit=False), client=None,
                      store=MemoryStore(), run_id="t")

    def fetch(board):
        registry = build_registry(ctx, board, AgentMemory(None),
                                  backend=SandboxBackend(SandboxEstate(people={})))
        return asyncio.run(registry.get("fetch_open_requests").run(limit=10))

    assert fetch(Blackboard()).startswith("no open requests")
    assert "none of this run's work items (999)" in fetch(Blackboard(scope={"999"}))


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
    # Written from the records in the CLI's format; no marker, no signature.
    assert estate.comments["1002"] == [
        "ALM access provisioning result :\n"
        "EF11111: BAO NGUYEN: User already present in JTS - no change needed - (active)"]
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


def test_an_approval_covers_only_the_users_on_the_card(tmp_path, scripted_recovery):
    """Review finding: one 'y' covered users added after the human approved."""
    seen = []

    def record(payload):
        from alm_agents.runner import ask_for_decision

        seen.append(sorted(i["userid"] for i in payload["items"]))
        # The real decision path: what the operator gets after typing "y".
        return ask_for_decision(payload, auto=True, console=Events(),
                                approver_prefix="test")

    llm = ScriptedLLM(
        ["triage", "validator", "risk_officer", "extractor", "provisioner", "DONE"], {
            "triage": [[("fetch_open_requests", {"limit": 10})]],
            "validator": [[("classify_user", {"userid": "AB12345"})]],
            "risk_officer": [[("request_human_approval",
                               {"reason": "one import", "userids": ["AB12345"]})]],
            # After the approval: a user recovered from 1002's malformed row.
            "extractor": [[("recover_user_ids", {"work_item_id": "1002"})]],
            "provisioner": [[("provision_jts_user", {"userid": "AB12345"})]],
        })
    report = asyncio.run(one_process(local_settings(tmp_path), llm, decide=record,
                                     thread_id="cover", scope=("1001", "1002")))
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



# ----------------------------------------------------- T2: comments from records

def test_the_closer_cannot_make_the_comment_claim_an_unrecorded_action(tmp_path):
    """Review critical gap: the closer's free text went straight onto the work item."""
    lie = "EF11111: BAO NGUYEN: User added to JTS"
    llm = ScriptedLLM(
        ["triage", "validator", "risk_officer", "verifier", "closer", "DONE"], {
            "triage": [[("fetch_open_requests", {"limit": 10})]],
            "validator": [[("classify_user", {"userid": "EF11111"})]],
            "risk_officer": [[("request_human_approval",
                               {"reason": "comment only", "userids": ["EF11111"]})]],
            "verifier": [[("check_jazz_permission", {"userid": "EF11111"})]],
            # A model (or text a requester planted) tries to dictate a false claim.
            "closer": [[("post_workitem_comment",
                         {"work_item_id": "1002", "text": lie, "userid": "EF11111"})]],
        })
    estate = SandboxEstate.default()
    asyncio.run(one_process(local_settings(tmp_path), llm, decide=approve,
                            thread_id="lie", scope=("1002",), estate=estate))
    posted = estate.comments["1002"]
    assert len(posted) == 1
    assert "User added to JTS" not in posted[0]
    assert "EF11111: BAO NGUYEN: User already present in JTS" in posted[0]


def test_evidence_goes_only_to_the_work_items_that_requested_the_user(tmp_path):
    """Sandbox run: 1001 received the profile screenshots of 1002's users."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    estate = SandboxEstate.default()
    ctx = ToolContext(settings=local_settings(tmp_path, commit=False), client=None,
                      store=MemoryStore(), run_id="t")
    registry = build_registry(ctx, Blackboard(), AgentMemory(None),
                              backend=SandboxBackend(estate),
                              shots_dir=str(tmp_path / "shots"))

    async def scenario():
        for work_item_id in ("1001", "1002"):
            await registry.get("fetch_work_item").run(work_item_id=work_item_id)
        captured = json.loads(await registry.get("capture_evidence").run(
            userids=["EF11111"]))
        wrong = await registry.get("attach_workitem_evidence").run(
            work_item_id="1001", userid="EF11111")
        unknown = await registry.get("attach_workitem_evidence").run(
            work_item_id="1001", userid="ZZ99999")
        return captured, wrong, unknown

    captured, wrong, unknown = asyncio.run(scenario())
    assert captured["attach_to"] == {"EF11111": ["1002"]}
    assert wrong.startswith("DENIED") and "requested on: 1002" in wrong
    assert unknown.startswith("DENIED")
    assert "EF11111.png" not in estate.attachments.get("1001", [])


def test_verify_all_users_checks_everyone_who_should_have_access(tmp_path):
    """Live eval: the verifier once skipped CD67890, who then got no evidence."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.models import Operation, Outcome, ProvisionResult
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    estate = SandboxEstate.default()
    board = Blackboard()
    ctx = ToolContext(settings=local_settings(tmp_path, commit=False), client=None,
                      store=MemoryStore(), run_id="t")
    registry = build_registry(ctx, board, AgentMemory(None), backend=SandboxBackend(estate))

    async def scenario():
        for work_item_id in ("1001", "1002"):
            await registry.get("fetch_work_item").run(work_item_id=work_item_id)
        for userid in ("AB12345", "CD67890", "EF11111", "GH22222"):
            await registry.get("classify_user").run(userid=userid)
        # This run reactivated CD67890 (done in the estate, recorded on the board).
        estate.people["CD67890"].archived = False
        board.results.append(ProvisionResult(
            userid="CD67890", operation=Operation.JTS_UNARCHIVE, outcome=Outcome.OK,
            work_item_id="1001", message="reactivated"))
        return json.loads(await registry.get("verify_all_users").run())

    result = asyncio.run(scenario())
    # EF11111 was already present; CD67890 was reactivated. AB12345 was never
    # provisioned and GH22222 is missing from LDAP, so neither is checked.
    assert result["verified"] == ["CD67890", "EF11111"]
    assert result["not_yet_verified"] == []
    assert board.verified == {"CD67890", "EF11111"}


def test_a_recovered_user_is_named_from_ldap_in_the_comment(tmp_path):
    """Sandbox run: a user recovered from free text was written 'TB22322: TB22322'."""
    from alm_agents.memory import MemoryStore as AgentMemory
    from alm_agents.toolkit import Blackboard, build_registry
    from alm_core.models import RequestedUser, SourceWorkItem
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    board = Blackboard()
    board.users["TB22322"] = RequestedUser(
        userid="TB22322", extracted_by_llm=True, extraction_confidence=0.9,
        source_work_items=[SourceWorkItem(work_item_id="1002", summary="")])
    ctx = ToolContext(settings=local_settings(tmp_path, commit=False), client=None,
                      store=MemoryStore(), run_id="t")
    registry = build_registry(ctx, board, AgentMemory(None),
                              backend=SandboxBackend(SandboxEstate.default()))

    async def scenario():
        await registry.get("classify_user").run(userid="TB22322")
        board.verified.add("TB22322")
        return json.loads(await registry.get("post_workitem_comment").run(
            work_item_id="1002"))

    posted = asyncio.run(scenario())["posted_text"]
    assert "TB22322: TB22322" not in posted
    assert "TOM BAKER" in posted.upper()


def test_no_comment_is_posted_before_anyone_is_verified(tmp_path):
    llm = ScriptedLLM(
        ["triage", "validator", "risk_officer", "closer", "DONE"], {
            "triage": [[("fetch_open_requests", {"limit": 10})]],
            "validator": [[("classify_user", {"userid": "AB12345"})]],
            "risk_officer": [[("request_human_approval",
                               {"reason": "x", "userids": ["AB12345"]})]],
            "closer": [[("post_workitem_comment", {"work_item_id": "1001"})]],
        })
    events = Events()
    estate = SandboxEstate.default()
    asyncio.run(one_process(local_settings(tmp_path), llm, decide=approve,
                            thread_id="early", estate=estate, console=events))
    observation = next(d["observation"] for k, d in events.events
                       if k == "tool_call" and d["tool"] == "post_workitem_comment")
    assert observation.startswith("ERROR: no verified user")
    assert estate.comments["1001"] == []


def test_agent_and_cli_comment_lines_have_the_same_format():
    import ewm_comment_workitems as cli
    from alm_agents.nodes.closure import render_comment

    agent = render_comment([("AB12345", "ALICE SMITH", "created")])
    cli_text = cli.build_comment([{"userid": "AB12345", "name": "ALICE SMITH"}],
                                 {"AB12345": {"action": "created", "state": "active"}})
    assert agent == cli_text



# --------------------------------------------------- T3: scope and id checks

def test_a_commit_run_must_name_its_work_items():
    with pytest.raises(local.SetupError, match="--commit needs --work-item"):
        local.check_commit_scope(local.parse_args(["--commit"]))
    local.check_commit_scope(local.parse_args(["--commit", "--work-item", "100001"]))
    local.check_commit_scope(local.parse_args([]))          # a dry run may scan
    local.check_commit_scope(local.parse_args(["--commit", "--resume", "local-1"]))


def test_a_commit_run_is_capped_at_a_few_work_items():
    many = [a for n in range(local.MAX_COMMIT_WORK_ITEMS + 1)
            for a in ("--work-item", str(1000 + n))]
    with pytest.raises(local.SetupError, match="at most"):
        local.check_commit_scope(local.parse_args(["--commit", *many]))


@pytest.mark.parametrize("bad", ["12a", "1 or 1=1", "../1", "12345678901", ""])
def test_work_item_ids_must_be_numbers_everywhere(bad):
    from pydantic import ValidationError

    from alm_agents.toolkit import AttachArgs, CommentArgs, RecoverArgs, WorkItemArgs
    from alm_core.errors import DataError
    from alm_core.tools import ewm

    with pytest.raises(SystemExit):
        local.parse_args(["--work-item", bad])
    for schema in (WorkItemArgs, RecoverArgs, CommentArgs):
        with pytest.raises(ValidationError):
            schema.model_validate({"work_item_id": bad})
    with pytest.raises(ValidationError):
        AttachArgs.model_validate({"work_item_id": bad, "userid": "AB12345"})
    with pytest.raises(DataError):
        ewm._fetch_one(None, bad)        # refused before any request is built



# ------------------------------------------------ T17: one password per run

def test_the_prompted_password_is_kept_for_the_whole_run():
    from alm_core.credentials import CredentialResolver

    class CountingPrompt:
        name = "interactive"
        calls = 0

        def get(self, key):
            CountingPrompt.calls += 1
            return "typed-once"

    resolver = CredentialResolver([CountingPrompt()], ttl=0)  # expires at once
    local.pin_password(resolver, "pw", resolver.get("pw"))
    for _ in range(3):
        assert resolver.get("pw", refresh=True) == "typed-once"  # a 403 re-auth
    assert CountingPrompt.calls == 1



# ------------------------------------------------------ T1: guided orchestration

GUIDED_SCRIPTS = {
    "triage": [[("fetch_open_requests", {"limit": 10})]],
    "validator": [[("classify_user", {"userid": "AB12345"})]],
    "risk_officer": [[("request_human_approval",
                       {"reason": "one import", "userids": ["AB12345"]})]],
    "provisioner": [[("provision_jts_user", {"userid": "AB12345"}),
                     ("request_ad_group_membership", {"userid": "AB12345"})]],
    "verifier": [[("check_jazz_permission", {"userid": "AB12345"})]],
    "evidence_officer": [
        [("capture_evidence", {"userids": ["AB12345"]})],
        [("attach_workitem_evidence", {"work_item_id": "1001", "userid": "AB12345"})]],
    "closer": [[("post_workitem_comment", {"work_item_id": "1001"})]],
}


def test_guided_mode_runs_the_routine_path_without_asking_the_supervisor(tmp_path):
    llm = ScriptedLLM([], GUIDED_SCRIPTS)   # a consulted supervisor would say DONE
    estate = SandboxEstate.default()
    report = asyncio.run(one_process(local_settings(tmp_path, orchestration="guided"),
                                     llm, decide=approve, thread_id="guided",
                                     estate=estate))
    assert not report["halted"], report["halt_reason"]
    assert llm.supervisor_calls == 0
    assert "AB12345" in estate.ad_requests
    assert estate.attachments["1001"] == ["AB12345.png"]
    assert len(estate.comments["1001"]) == 1


def test_guided_mode_asks_the_supervisor_when_an_agent_is_refused(tmp_path):
    scripts = {**GUIDED_SCRIPTS,
               # Tries to write before approval: a real (non-dry-run) refusal.
               "validator": [[("classify_user", {"userid": "AB12345"}),
                              ("provision_jts_user", {"userid": "AB12345"})]]}
    llm = ScriptedLLM(["risk_officer", "DONE"], scripts)
    asyncio.run(one_process(local_settings(tmp_path, orchestration="guided"), llm,
                            decide=approve, thread_id="guided-exc"))
    assert llm.supervisor_calls >= 1


def test_guided_dry_run_denials_do_not_need_the_supervisor(tmp_path):
    llm = ScriptedLLM([], GUIDED_SCRIPTS)
    report = asyncio.run(one_process(
        local_settings(tmp_path, commit=False, orchestration="guided"), llm,
        decide=approve, thread_id="guided-dry"))
    assert llm.supervisor_calls == 0
    assert report["policy"]["writes"] == 0


def test_a_refused_agent_is_not_counted_as_done():
    from alm_agents.supervisor import guided_next

    history = [
        {"agent": "triage", "stopped": "finish", "denials": 0},
        {"agent": "closer", "stopped": "finish", "denials": 1},
        {"agent": "approval", "stopped": "approved", "denials": 0},
        {"agent": "risk_officer", "stopped": "finish", "denials": 0},
    ]
    snapshot = {"verified": ["AB12345"], "approved": True}
    names = []
    for _ in range(8):
        decision = guided_next(history, snapshot, shadow=False)
        names.append(decision.next_agent)
        if decision.done:
            break
        history.append({"agent": decision.next_agent, "stopped": "finish", "denials": 0})
    assert "closer" in names and names[-1] == "DONE"
    assert guided_next([{"agent": "validator", "stopped": "iteration limit"}],
                       snapshot, shadow=False) is None


# ------------------------------------------------------ T4: names stay local

def test_personal_names_never_reach_the_model_but_stay_on_the_console(tmp_path):
    llm = RecordingLLM(["triage", "validator", "DONE"], {
        "triage": [[("fetch_work_item", {"work_item_id": "1001"})]],
        "validator": [[("classify_user", {"userid": "AB12345"})]]})
    events = Events()
    asyncio.run(one_process(local_settings(tmp_path, commit=False), llm,
                            decide=approve, thread_id="names", console=events))
    shown = " ".join(m.content for m in llm.seen if isinstance(m, ToolMessage))
    for name in ("ALICE", "SMITH", "CLAIRE", "DUPONT", "Alice Smith"):
        assert name.lower() not in shown.lower(), name
    assert "AB12345" in shown and "[name]" in shown
    console_view = " ".join(d["observation"] for k, d in events.events
                            if k == "tool_call")
    assert "SMITH" in console_view


def test_name_redaction_keeps_ordinary_words_and_user_ids():
    from alm_core.logging import redact_names

    text = "USER: AB12345 GHOST USER - User already present in JTS"
    assert redact_names(text, ["GHOST", "USER"]) == (
        "USER: AB12345 [name] USER - User already present in JTS")
    assert redact_names("nothing to hide", []) == "nothing to hide"



# ------------------------------------------- T5: one report per user, either tool

CLI_COMMENT = ("ALM access provisioning result :<br/>AB12345: ALICE SMITH: User added to "
               "JTS - (active)<br/>[alm-cli:1001:abc123]")


def test_a_user_reported_by_the_cli_is_not_reported_again_by_the_agents(tmp_path):
    estate = SandboxEstate.default()
    estate.comments["1001"].append(CLI_COMMENT)
    llm = ScriptedLLM([], GUIDED_SCRIPTS)
    asyncio.run(one_process(local_settings(tmp_path, orchestration="guided"), llm,
                            decide=approve, thread_id="cli-first", estate=estate))
    # The CLI's line already says AB12345 was added: the agents add nothing.
    assert estate.comments["1001"] == [CLI_COMMENT]


def test_the_cli_skips_a_work_item_the_agents_already_reported():
    import ewm_comment_workitems as cli
    from alm_agents.nodes.closure import render_comment

    agent_comment = render_comment([("AB12345", "ALICE SMITH", "created")])
    stored = agent_comment.replace("\n", "<br/>")        # how EWM keeps it
    members = [{"userid": "AB12345", "name": "ALICE SMITH"}]
    created = {"AB12345": {"action": "created", "state": "active"}}
    assert cli.all_reported([stored], members, created)
    # A different outcome for the same user is new information, not a duplicate.
    already = {"AB12345": {"action": "already_active", "state": "active"}}
    assert not cli.all_reported([stored], members, already)
    assert not cli.all_reported([], members, created)


# ----------------------------------------------------------- T6: run metrics

def test_the_report_says_what_the_run_cost(tmp_path):
    guided = asyncio.run(one_process(
        local_settings(tmp_path / "g", orchestration="guided"),
        ScriptedLLM([], GUIDED_SCRIPTS), decide=approve, thread_id="m-guided"))
    metrics = guided["metrics"]
    assert metrics["supervisor_model_calls"] == 0
    assert metrics["agent_model_calls"] > 0
    assert metrics["model_calls"] == metrics["agent_model_calls"]
    assert metrics["per_agent"]["provisioner"]["tool_calls"] >= 2
    assert metrics["hops"] == guided["hops"]

    plan = ["triage", "validator", "risk_officer", "provisioner", "verifier",
            "evidence_officer", "closer", "DONE"]
    agentic = asyncio.run(one_process(
        local_settings(tmp_path / "a"), ScriptedLLM(plan, GUIDED_SCRIPTS),
        decide=approve, thread_id="m-agentic"))
    assert agentic["metrics"]["supervisor_model_calls"] == agentic["hops"]


# --------------------------------------------- T10: comment ledger key by content

def test_new_comment_content_is_posted_and_repeated_content_is_a_replay(tmp_path):
    from alm_core.store.memory import MemoryStore
    from alm_core.tools.base import ToolContext

    estate = SandboxEstate.default()
    backend = SandboxBackend(estate)
    ctx = ToolContext(settings=local_settings(tmp_path), client=None,
                      store=MemoryStore(), run_id="r")
    ctx.approval = ApprovalDecision(thread_id="t", approved=True, approver="test:me",
                                    approved_userids=["AB12345"])

    async def post(text):
        return await backend.post_comment(ctx, work_item_id="1001", userid="AB12345",
                                          text=text, marker="")

    async def scenario():
        first = await post("AB12345: ALICE SMITH: User added to JTS - (active)")
        later = await post("AB12345: ALICE SMITH: evidence attached - (active)")
        again = await post("AB12345: ALICE SMITH: evidence attached - (active)")
        return first, later, again

    first, later, again = asyncio.run(scenario())
    assert (first.replayed, later.replayed, again.replayed) == (False, False, True)
    assert len(estate.comments["1001"]) == 2


# ------------------------------------------- T16/T22: resume and install errors

def test_resume_last_finds_the_most_recent_run(tmp_path):
    settings = local_settings(tmp_path)
    assert local.last_thread(settings) == ""
    local.remember_last_thread(settings, "local-1234abcd")
    assert local.last_thread(settings) == "local-1234abcd"


def test_a_missing_package_prints_the_install_command(monkeypatch, capsys):
    import alm_agents.sandbox as sandbox

    def missing(**_kw):
        raise ModuleNotFoundError("No module named 'pydantic_settings'",
                                  name="pydantic_settings")

    monkeypatch.setattr(sandbox, "load_env", lambda: None)   # never read the real .env
    monkeypatch.setattr(local, "build_settings", missing)
    assert local.main(["--check"]) == 2
    out = capsys.readouterr().out
    assert "pydantic_settings" in out and "-m pip install -r requirements-cloud.txt" in out



# ------------------------------------------------ T12: GPT outcome ambiguity

@pytest.fixture
def gpt_backend(monkeypatch):
    import alm_worker.gpt as gpt

    FakeGpt.instances, FakeGpt.closed = [], 0
    FakeGpt.reply = "Your request has been submitted correctly. Failed Requests: 0"
    FakeGpt.click_raises = False
    monkeypatch.setattr(gpt, "GptSession", FakeGpt)
    monkeypatch.setattr(local, "cdp_reachable", lambda _url, timeout=3.0: True)
    backend = local.make_local_backend(cdp_url="http://127.0.0.1:9222",
                                       gpt_url="https://gpt.example/home.jsf",
                                       ad_label="inetpsa.com")
    yield backend
    backend.close()


def _twice(backend, ctx, user):
    async def scenario():
        first = await backend.request_group_membership(ctx, user, group="G", domain="D")
        second = await backend.request_group_membership(ctx, user, group="G", domain="D")
        return first, second
    return asyncio.run(scenario())


def test_a_crash_after_modify_is_never_retried_automatically(tmp_path, gpt_backend):
    FakeGpt.click_raises = True
    ctx, user = _gpt_ctx(tmp_path)
    first, second = _twice(gpt_backend, ctx, user)
    assert first.outcome.value == "failed" and first.detail.get("outcome_unknown")
    assert "Pending Requests" in first.message
    assert second.replayed                    # the ledger holds it; GPT is not asked again
    assert sum(len(s.added) for s in FakeGpt.instances) == 1
    assert FakeGpt.closed >= 1                # the broken session was dropped


def test_an_unreadable_gpt_reply_is_unknown_not_rejected(tmp_path, gpt_backend):
    FakeGpt.reply = "Session expired. Please reload."
    ctx, user = _gpt_ctx(tmp_path)
    first, second = _twice(gpt_backend, ctx, user)
    assert first.detail.get("outcome_unknown") and second.replayed


def test_an_explicit_gpt_rejection_may_be_retried(tmp_path, gpt_backend):
    FakeGpt.reply = "Failed Requests: 1"
    ctx, user = _gpt_ctx(tmp_path)
    first, second = _twice(gpt_backend, ctx, user)
    assert first.outcome.value == "failed" and not first.detail.get("outcome_unknown")
    assert not second.replayed                # a real rejection is safe to try again
    assert sum(len(s.added) for s in FakeGpt.instances) == 2


def test_a_locked_ledger_refuses_the_write_cleanly(tmp_path):
    import sqlite3

    from alm_core.errors import IdempotencyViolation
    from alm_core.models import Operation
    from alm_core.store.sqlite import SqliteStore

    async def scenario():
        store = SqliteStore(str(tmp_path / "l.db"))
        await store.start()
        await store.migrate()
        original = store._conn().execute

        async def locked(sql, *args):
            if sql.startswith("BEGIN"):
                raise sqlite3.OperationalError("database is locked")
            return await original(sql, *args)

        store._conn().execute = locked
        try:
            await store.claim("k", run_id="r", work_item_id="1001", userid="AB12345",
                              operation=Operation.JTS_CREATE)
        finally:
            store._conn().execute = original
            await store.close()

    with pytest.raises(IdempotencyViolation, match="locked by another process"):
        asyncio.run(scenario())



# ------------------------------------------------ T21: ledger path from .env

def test_the_ledger_path_comes_from_the_flag_then_env_then_default(env, monkeypatch,
                                                                    tmp_path):
    env(TEST_EWM, TEST_JTS)
    monkeypatch.delenv("ALM_LEDGER_PATH", raising=False)
    assert local.build_settings(commit=False).ledger_path == str(local.DEFAULT_LEDGER)
    monkeypatch.setenv("ALM_LEDGER_PATH", str(tmp_path / "from-env.db"))
    assert local.build_settings(commit=False).ledger_path == str(tmp_path / "from-env.db")
    flag = str(tmp_path / "from-flag.db")
    assert local.build_settings(commit=False, ledger_path=flag).ledger_path == flag


def test_looking_up_an_unrequested_id_does_not_put_it_on_the_card(tmp_path):
    """A model naming a well-formed ID that no work item requested gets a read,
    not a user: the ID never reaches the approval card or a write."""
    seen = []

    def record(payload):
        seen.append(sorted(i["userid"] for i in payload["items"]))
        return approve(payload)

    llm = ScriptedLLM(["triage", "validator", "risk_officer", "DONE"], {
        "triage": [[("fetch_open_requests", {"limit": 10})]],
        # TB22322 is on 1002, which is outside this run's scope (1001).
        "validator": [[("classify_user", {"userid": "AB12345"}),
                       ("classify_user", {"userid": "TB22322"})]],
        "risk_officer": [[("request_human_approval",
                           {"reason": "one import", "userids": ["AB12345", "TB22322"]})]],
    })
    events = Events()
    asyncio.run(one_process(local_settings(tmp_path), llm, decide=record,
                            thread_id="unrequested", console=events))
    assert seen and all("TB22322" not in card for card in seen)
    lookup = [d for k, d in events.events
              if k == "tool_call" and d["args"].get("userid") == "TB22322"]
    assert "NOT added to the run" in lookup[0]["observation"]
