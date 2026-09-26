"""Run the multi-agent system on this machine against the real EWM, JTS and GPT.

No cloud: OSLC over your network, Gemini through your API key, GPT through the
debug Chrome you already sign in to for the CLI, and every durable record in
one SQLite file under ``out/local/``.

    python src/agent_local.py --check                    # is everything reachable?
    python src/agent_local.py --work-item 123456          # dry run: plan, write nothing
    python src/agent_local.py --work-item 123456 --commit # write, after your y/N
    python src/agent_local.py --resume <thread-id>        # continue a paused/crashed run

It keeps the CLI's safety model: a dry run unless ``--commit``, the CLI's own
TEST/PROD detection and typed ``PROD`` confirmation, the CLI's TLS settings
(``ALM_CA_BUNDLE`` / ``ALM_TLS_VERIFY``), and your password prompted, never
stored. On top of that, every agent write passes the policy engine, your
approval at the terminal, and the idempotency ledger.

Only standard-library imports at module level: logging must be quietened
before any ``alm_core`` module configures it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "out" / "local"
DEFAULT_LEDGER = OUT_DIR / "alm.db"
DEFAULT_GPT_URL = "https://gpt.fiatspa.com/GlobalProvisioningTool/home.jsf"


class SetupError(Exception):
    """A configuration problem to show the operator plainly, then stop."""


# ------------------------------------------------------------------ backend

def cdp_reachable(url: str, timeout: float = 3.0) -> bool:
    """True when the debug Chrome is listening. Raw socket: never via the proxy."""
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1",
                                       parsed.port or 9222), timeout):
            return True
    except OSError:
        return False


def make_local_backend(*, cdp_url: str, gpt_url: str, ad_label: str):
    """The live OSLC backend, with the AD step done in your signed-in Chrome.

    Built lazily so this module imports without the cloud extras installed.
    """
    from alm_core.errors import WorkerUnavailable
    from alm_core.models import Operation
    from alm_core.tools.base import guarded_write

    from .toolkit import LiveBackend

    class LocalBackend(LiveBackend):
        """Everything over OSLC except the AD group, which goes through GPT.

        In the cloud design the AD step is a Pub/Sub job for a Windows worker.
        Here the same GPT page actions (``alm_worker.gpt.GptSession``) run in
        the debug Chrome from ``scripts/start-gpt.ps1`` - the path the CLI uses
        in production. Playwright's sync API is bound to the thread that
        started it, so every GPT call runs on one dedicated thread.
        """

        def __init__(self):
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpt")
            self._session = None

        def _add_member(self, userid: str, group: str, domain: str) -> tuple[bool, str]:
            if self._session is None:
                if not cdp_reachable(cdp_url):
                    raise WorkerUnavailable(
                        f"no debug Chrome at {cdp_url}. Run scripts\\start-gpt.ps1, sign "
                        "in to GPT in that window, then resume this run.")
                from alm_worker.gpt import GptSession

                session = GptSession(url=gpt_url, ad_label=ad_label)
                session.attach(cdp_url)
                self._session = session
            return self._session.add_member(userid=userid, group=group, domain=domain)

        async def request_group_membership(self, ctx, user, *, group, domain):
            work_item_id = user.work_item_ids[0] if user.work_item_ids else ""

            async def submit():
                ok, message = await asyncio.wrap_future(self._executor.submit(
                    self._add_member, user.userid, group, domain))
                return ok, message, {"group": group, "domain": domain, "via": "gpt-cdp"}

            return await guarded_write(
                ctx, userid=user.userid, work_item_id=work_item_id,
                operation=Operation.AD_GROUP_ADD, step="ad_provision", action=submit)

        def close(self) -> None:
            if self._session is not None:
                try:
                    self._executor.submit(self._session.close).result(timeout=30)
                except Exception:  # noqa: BLE001 - closing must not mask the run result
                    pass
                self._session = None
            self._executor.shutdown(wait=False)

    return LocalBackend()


# ------------------------------------------------------------------ settings

def build_settings(*, commit: bool, model: str = "", rpm: float = 0.0,
                   ledger_path: str = ""):
    """Settings for a local run, with the environment and TLS decided the CLI's way."""
    import alm_config
    from alm_core.config import Settings

    mismatch = alm_config.env_mismatch()
    if mismatch:
        raise SetupError(f"{mismatch}. Point both servers at the same environment.")
    environment = alm_config.alm_env()
    if environment == "UNKNOWN":
        raise SetupError("cannot tell TEST from PROD from EWM_SERVER / JTS_SERVER. "
                         "Set ALM_ENV=TEST or ALM_ENV=PROD in .env.")
    # Guided by default: the fixed order runs the routine steps and the
    # supervisor model is consulted only when something goes wrong.
    orchestration = os.getenv("ALM_ORCHESTRATION", "").strip().lower() or "guided"
    if orchestration not in ("guided", "agentic"):
        raise SetupError(f"ALM_ORCHESTRATION={orchestration} is not supported by local "
                         "runs. Use guided (default) or agentic.")
    try:
        verify = alm_config.tls_verify()
    except SystemExit as err:  # the CLI helper stops with a message; show it instead
        raise SetupError(str(err)) from err

    overrides: dict = {
        "environment": environment, "orchestration": orchestration, "llm_enabled": True,
        "postgres_dsn": "", "ledger_path": ledger_path or str(DEFAULT_LEDGER),
        "shadow_mode": not commit,
        # TLS exactly as the CLI resolves it: a CA bundle path, the default
        # trust store, or - TEST only - unverified with the CLI's warning.
        "ca_bundle": verify if isinstance(verify, str) else "",
        "tls_insecure": verify is False,
    }
    if model:
        overrides["agent_model"] = model
        overrides["supervisor_model"] = ""
    if rpm:
        overrides["llm_requests_per_minute"] = rpm
    try:
        return Settings(**overrides)
    except Exception as err:  # pydantic ValidationError: show the reason, not a trace
        raise SetupError(str(err)) from err


def gpt_target() -> dict:
    """GPT settings, read from the names the CLI's .env already uses."""
    return {"cdp_url": os.getenv("CDP_URL", "http://127.0.0.1:9222"),
            "gpt_url": os.getenv("GPT_URL", DEFAULT_GPT_URL),
            "ad_label": os.getenv("AD_LABEL", "inetpsa.com")}


# --------------------------------------------------------------------- check

async def check(settings, console, work_item_ids: list[str]) -> int:
    """Everything a run needs, verified read-only. Writes nothing anywhere."""
    from alm_core.auth import JazzClient
    from alm_core.credentials import build_resolver
    from alm_core.logging import scrub_secrets
    from alm_core.store.memory import MemoryStore
    from alm_core.tools import ewm
    from alm_core.tools.base import ToolContext

    from .sandbox import check as model_check

    failures = 0

    def ok(label: str, detail: str) -> None:
        console.line(f"OK    {label:14} {detail}")

    def fail(label: str, detail: str) -> None:
        nonlocal failures
        failures += 1
        console.line(f"FAIL  {label:14} {scrub_secrets(detail)[:300]}")

    tls = (settings.ca_bundle or "default trust store") if not settings.tls_insecure \
        else "UNVERIFIED (TEST only)"
    console.line(f"environment   {settings.environment}   TLS {tls}")
    console.line(f"EWM           {settings.ewm_server or '(EWM_SERVER not set)'}")
    console.line(f"JTS           {settings.jts_server or '(JTS_SERVER not set)'}")
    console.line(f"ledger        {settings.ledger_path}")
    console.line("")

    # 1. The model: key, availability, function calling.
    if await asyncio.to_thread(model_check, settings, console, "") != 0:
        failures += 1

    # 2. EWM and JTS: DNS, TLS and form login, with the CLI's credentials.
    if not (settings.ewm_server and settings.jts_server and settings.service_account):
        fail("config", "EWM_SERVER, JTS_SERVER and CID must all be set in .env")
        return 2
    resolver = build_resolver(settings, prompt="Jazz password")
    client = JazzClient(settings, resolver)
    try:
        from alm_core.errors import AuthenticationError

        for kind, server in (("ewm", settings.ewm_server), ("jts", settings.jts_server)):
            try:
                await asyncio.to_thread(client.session, server, kind=kind)
                ok(f"{kind.upper()} login", f"signed in as {settings.service_account}")
            except AuthenticationError as err:
                fail(f"{kind.upper()} login", f"{err.message}")
                # Every further login would repeat the same rejected password and
                # count towards an account lockout.
                console.line("      stopping here: re-run --check and retype the password")
                return 2
            except Exception as err:  # noqa: BLE001 - reported, not raised
                fail(f"{kind.upper()} login", f"{type(err).__name__}: {err}")

        # 3. One read-only OSLC query - the agents' first tool call.
        ctx = ToolContext(settings=settings, client=client, store=MemoryStore(), run_id="check")
        try:
            if work_item_ids:
                for work_item_id in work_item_ids:
                    item = await ewm.fetch_work_item(ctx, work_item_id)
                    if item is None:
                        fail("OSLC", f"work item {work_item_id} not found in the project area")
                    else:
                        ok("OSLC", f"{work_item_id}: {len(item.users)} user(s) parsed, "
                                   f"state {item.state or '?'}")
            else:
                items = await ewm.fetch_open_requests(ctx, 5)
                ok("OSLC", f"queue query answered ({len(items)} shown of the first page)")
        except Exception as err:  # noqa: BLE001
            fail("OSLC", f"{type(err).__name__}: {err}")
    finally:
        client.close()

    # 4. GPT: the debug Chrome the AD step attaches to.
    target = gpt_target()
    try:
        import playwright  # noqa: F401 - the GPT step drives Chrome through it
    except ModuleNotFoundError:
        fail("GPT Chrome", "playwright is not installed, so the AD step cannot drive "
                           "Chrome: python -m pip install -r requirements-cloud.txt")
    if cdp_reachable(target["cdp_url"]):
        ok("GPT Chrome", f"listening on {target['cdp_url']} - make sure GPT is signed in")
    else:
        fail("GPT Chrome", f"nothing on {target['cdp_url']}. Run scripts\\start-gpt.ps1 and "
                           "sign in to GPT (only needed for runs that add AD groups)")

    # 5. The browser that captures evidence screenshots.
    from alm_core.tools.evidence import browser_channel

    channel = browser_channel()
    label = {"msedge": "Microsoft Edge", "": "Playwright Chromium"}.get(channel, channel)
    try:
        await asyncio.to_thread(_launch_browser, channel)
        ok("browser", f"{label} starts - evidence screenshots will use it")
    except ModuleNotFoundError:
        fail("browser", "playwright is not installed: "
                        "python -m pip install -r requirements-cloud.txt")
    except Exception as err:  # noqa: BLE001
        fail("browser", f"{label} did not start ({type(err).__name__}: "
                        f"{str(err).splitlines()[0][:160]}). Set ALM_BROWSER_CHANNEL, or run "
                        "'python -m playwright install chromium'")

    # 6. The ledger file can be created and written.
    try:
        from alm_core.store.sqlite import SqliteStore

        store = SqliteStore(settings.ledger_path)
        await store.start()
        await store.migrate()
        await store.close()
        ok("ledger", "SQLite ledger ready")
    except Exception as err:  # noqa: BLE001
        fail("ledger", f"{type(err).__name__}: {err}")

    console.line("")
    if failures:
        console.line(f"{failures} check(s) failed - fix those before a run.")
        return 2
    console.line("Ready. Start with a dry run:  python src/agent_local.py --work-item <id>")
    return 0


def _launch_browser(channel: str) -> None:
    """Start and stop the capture browser once, headless. Touches no server."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        kwargs = {"headless": True}
        if channel:
            kwargs["channel"] = channel
        playwright.chromium.launch(**kwargs).close()


class _PinnedSecret:
    """Answers one secret from memory for the life of the process."""

    name = "operator prompt (this run)"

    def __init__(self, key: str, value: str):
        self.key, self.value = key, value

    def get(self, key: str) -> str | None:
        return self.value if key == self.key else None


def pin_password(resolver, key: str, value: str) -> None:
    """Make ``value`` the answer for ``key`` until the process exits."""
    resolver.providers.insert(0, _PinnedSecret(key, value))
    resolver.ttl = float("inf")


# ----------------------------------------------------------------------- run

async def run(settings, args, console) -> dict:
    """One real run, start to finish, including every approval pause."""
    from alm_core.auth import JazzClient
    from alm_core.credentials import build_resolver
    from alm_core.store import get_store
    from alm_core.tools.base import ToolContext

    from . import llm as llm_module
    from .agentic import AgenticRuntime, build_agentic_graph
    from .graph import checkpointer_for
    from .memory import MemoryStore as AgentMemory
    from .runner import ask_for_decision, drive

    agent_llm = llm_module.get_agent_llm(settings)
    if agent_llm is None:
        raise SetupError("no Gemini client - run with --check to see why")

    resolver = build_resolver(settings, prompt="Jazz password")
    # Ask for the password now, not halfway through the first agent's output,
    # and keep it for the whole run: no re-prompt after the cache TTL or on a
    # 403, where a mistyped answer from a worker thread would count towards an
    # account lockout.
    pin_password(resolver, settings.password_secret_name,
                 resolver.get(settings.password_secret_name))

    client = JazzClient(settings, resolver)
    # Sign in to both servers now. A wrong password must stop the run here,
    # once - not surface in every agent's tool calls, each retrying the login
    # and counting towards an account lockout.
    from alm_core.errors import AlmError

    for kind, server in (("ewm", settings.ewm_server), ("jts", settings.jts_server)):
        try:
            await asyncio.to_thread(client.session, server, kind=kind)
        except AlmError as err:
            client.close()
            raise SetupError(f"{kind.upper()} sign-in failed: {err.message}") from err

    store = await get_store(settings)
    backend = make_local_backend(**gpt_target())
    ctx = ToolContext(settings=settings, client=client, store=store, run_id="")
    thread_id = args.resume or f"local-{uuid.uuid4().hex[:8]}"

    def decide(payload):
        if settings.shadow_mode:
            # A dry run cannot write, so there is nothing to decide: show the card
            # as a preview of what --commit will ask, and let the plan continue.
            return ask_for_decision(
                payload, auto=True, console=console, approver_prefix="dry-run",
                auto_note="dry run: this is the card --commit will ask you to approve")
        return ask_for_decision(payload, auto=args.auto_approve, console=console,
                                approver_prefix="local")

    try:
        async with checkpointer_for(settings) as checkpointer:
            runtime = AgenticRuntime(
                ctx, llm=agent_llm, supervisor_llm=llm_module.get_supervisor_llm(settings),
                memory=AgentMemory(None), max_hops=settings.max_hops, backend=backend,
                on_event=console, shots_dir=str(OUT_DIR / "evidence" / thread_id))
            graph = build_agentic_graph(runtime, checkpointer=checkpointer)
            report = await drive(graph, ctx, thread_id=thread_id, decide=decide,
                                 console=console, resume=bool(args.resume),
                                 work_item_ids=list(args.work_item or []), trigger="local")
            report["policy"] = runtime.policy.summary()
            report["shadow"] = settings.shadow_mode
            report["environment"] = settings.environment
            return report
    finally:
        backend.close()
        client.close()
        await store.close()


# ---------------------------------------------------------------------- main

# A committing run touches only work items the operator named, and few of
# them: the run's write and hop budgets are sized for a handful of requests.
MAX_COMMIT_WORK_ITEMS = 5


def _work_item_id(value: str) -> str:
    value = value.strip()
    if not value.isdigit() or len(value) > 10:
        raise argparse.ArgumentTypeError(f"{value!r} is not an EWM work item number")
    return value


def check_commit_scope(args) -> None:
    """A run that writes names the work items it may touch, and only a few."""
    if not args.commit or args.resume:
        return  # a resumed run keeps the scope it started with
    if not args.work_item:
        raise SetupError(
            "--commit needs --work-item <id>: a run that writes must name the work "
            "items it may touch. Dry runs (no --commit) may scan the queue.")
    if len(args.work_item) > MAX_COMMIT_WORK_ITEMS:
        raise SetupError(
            f"--commit takes at most {MAX_COMMIT_WORK_ITEMS} work items per run "
            f"({len(args.work_item)} given). Split them into several runs.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent_local",
        description="Run the ALM multi-agent system locally against EWM, JTS and GPT.")
    parser.add_argument("--check", action="store_true",
                        help="verify Gemini, EWM/JTS login, OSLC, GPT Chrome and the ledger")
    parser.add_argument("--work-item", action="append", metavar="ID",
                        type=_work_item_id,
                        help="limit the run to this work item (repeatable; required "
                             f"with --commit, at most {MAX_COMMIT_WORK_ITEMS})")
    parser.add_argument("--commit", action="store_true",
                        help="perform the writes (default: dry run - plan and record only)")
    parser.add_argument("--resume", metavar="THREAD_ID", default="",
                        help="continue a paused or interrupted run")
    parser.add_argument("--auto-approve", action="store_true",
                        help="approve at the gate without asking (TEST only)")
    parser.add_argument("--ledger", default="",
                        help=f"SQLite ledger file (default {DEFAULT_LEDGER})")
    parser.add_argument("--model", default="", help="override ALM_AGENT_MODEL")
    parser.add_argument("--rpm", type=float, default=0.0,
                        help="override ALM_LLM_REQUESTS_PER_MINUTE")
    parser.add_argument("--verbose", action="store_true",
                        help="full tool observations and the service logs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from .sandbox import load_env

    load_env()
    os.environ["ALM_LOG_LEVEL"] = "INFO" if args.verbose else "ERROR"
    logging.getLogger().setLevel(logging.INFO if args.verbose else logging.ERROR)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    from .runner import Console, RunModeMismatch, print_report, save_report

    console = Console(verbose=args.verbose)
    try:
        settings = build_settings(commit=args.commit, model=args.model, rpm=args.rpm,
                                  ledger_path=args.ledger)
        if settings.tls_insecure:
            # alm_config has already printed its single [warn]; urllib3 would
            # otherwise repeat it for every request. The CLI does the same.
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if args.check:
            return asyncio.run(check(settings, console, list(args.work_item or [])))

        check_commit_scope(args)
        if args.auto_approve and settings.is_prod:
            raise SetupError("--auto-approve is refused against PRODUCTION")
        if args.commit and settings.is_prod:
            import alm_config

            if not alm_config.confirm_prod_write("This agent run"):
                return 1

        mode = "WRITES ENABLED" if args.commit else "DRY RUN (nothing is written)"
        scope = ", ".join(args.work_item or []) or "the whole active queue"
        console.line(f"ALM agents, local - {settings.environment} - {mode}")
        console.line(f"model {settings.llm_provider}:{settings.agent_model}   "
                     f"orchestration {settings.orchestration}   scope {scope}")
        if not args.work_item and not args.resume:
            console.line("tip: limit a first run with --work-item <id>")

        report = asyncio.run(run(settings, args, console))
    except SetupError as err:
        console.line(f"setup: {err}")
        return 2
    except RunModeMismatch as err:
        console.line(f"resume refused: {err}")
        return 2
    except LookupError as err:
        console.line(f"resume: {err}. The thread id is shown when a run starts and "
                     "in its report under out/local/.")
        return 2
    except KeyboardInterrupt:
        console.line("\ninterrupted. Continue later with --resume <thread-id> "
                     "(shown in out/local/ reports and the last run line).")
        return 130

    print_report(report, console)
    path = save_report(report, OUT_DIR)
    console.line("")
    console.line(f"full report and audit trail: {path}")
    if report["halted"]:
        console.line("next: fix the cause in the HALTED line above, then run again")
    elif not args.commit:
        console.line("next: review the plan above; to perform it, run again with --commit")
    return 1 if report["halted"] and "rejected" not in report["halt_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
