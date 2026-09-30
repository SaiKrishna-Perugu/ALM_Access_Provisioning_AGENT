"""Run the real multi-agent system against a simulated ALM estate, on a laptop.

The production tools reach EWM, the JTS registry, LDAP, Pub/Sub and a browser
on the corporate network. None of that is reachable from a laptop, so without
this module the agents could only ever be exercised in the client environment.

What is simulated, and what is not:

* **Simulated** - the estate behind the tools: five work-item users in known
  states, the JTS directory, the AD queue (applied instantly), the profile
  screenshots. See :class:`SandboxEstate`.
* **Real** - everything that decides and everything that guards: the Gemini
  model, the nine agents and their prompts, the supervisor, the policy engine,
  the approval interrupt, ``guarded_write`` (shadow mode, approval, idempotency
  ledger, audit), and the evidence validation gate.

So a sandbox run answers "do the agents behave?" honestly, and says nothing at
all about whether the corporate endpoints are reachable - that is what
``alm_core.smoke`` is for.

    python src/agent_sandbox.py --check          # key, model list, one tool call
    python src/agent_sandbox.py                  # a full run; you approve at the gate
    python src/agent_sandbox.py --auto-approve   # unattended
    python src/agent_sandbox.py --shadow         # plan only, no simulated writes

Only standard-library imports at module level: logging must be quietened
before any ``alm_core`` module configures it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import struct
import sys
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .runner import Console, ask_for_decision, drive
from .runner import print_report as runner_print_report
from .runner import save_report as runner_save_report

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "out" / "sandbox"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


# ---------------------------------------------------------------- the estate

@dataclass
class Person:
    userid: str
    name: str
    email: str
    in_ldap: bool = True
    contributor: bool = False
    archived: bool = False
    roles: list[str] = field(default_factory=list)


@dataclass
class SandboxEstate:
    """A small, deliberately awkward company.

    Each user exercises a different path through the agents:

    * AB12345 - in LDAP, not yet in JTS: needs importing.
    * CD67890 - an archived contributor: must be *reactivated*, not re-created.
    * EF11111 - already active with the role: nothing to do, and nothing may be
      claimed in the comment.
    * GH22222 - not in LDAP at all: cannot be provisioned; must be reported.
    * TB22322 - only present inside a malformed row, so the deterministic parser
      rejects it and the extractor has to recover it.
    """

    people: dict[str, Person] = field(default_factory=dict)
    work_items: dict[str, dict] = field(default_factory=dict)
    comments: dict[str, list[str]] = field(default_factory=dict)
    attachments: dict[str, list[str]] = field(default_factory=dict)
    ad_requests: list[str] = field(default_factory=list)
    role: str = "JazzUsers"

    @classmethod
    def default(cls, role: str = "JazzUsers") -> SandboxEstate:
        people = [
            Person("AB12345", "Alice Smith", "alice.smith@example.com"),
            Person("CD67890", "Claire Dupont", "claire.dupont@example.com",
                   contributor=True, archived=True, roles=[role]),
            Person("EF11111", "Bao Nguyen", "bao.nguyen@example.com",
                   contributor=True, roles=[role]),
            Person("GH22222", "", "", in_ldap=False),
            Person("TB22322", "Tom Baker", "tom.baker@example.com"),
        ]
        work_items = {
            "1001": {
                "summary": "ALM access - powertrain calibration team",
                "justification": "New starters on the calibration programme.",
                "new_users": ("SMITH,ALICE,alice.smith@example.com,AB12345;"
                              "DUPONT,CLAIRE,claire.dupont@example.com,CD67890;"),
            },
            "1002": {
                "summary": "ALM access - validation team",
                "justification": "Validation engineers joining the release train.",
                "new_users": ("NGUYEN,BAO,bao.nguyen@example.com,EF11111;"
                              "GHOST,USER,ghost.user@example.com,GH22222;"
                              "please also add Tom Baker (TB22322), thanks;"),
            },
        }
        return cls(people={p.userid: p for p in people}, work_items=work_items,
                   comments={k: [] for k in work_items},
                   attachments={k: [] for k in work_items}, role=role)

    def has_role(self, userid: str) -> bool:
        person = self.people.get(userid)
        return bool(person and person.contributor and not person.archived
                    and self.role in person.roles)


class SandboxBackend:
    """The ``toolkit.Backend`` protocol over a :class:`SandboxEstate`.

    Reads answer from the estate. Writes go through the real ``guarded_write``,
    so shadow mode, the approval check, the idempotency ledger and the audit
    trail behave exactly as they do in production; only the action inside the
    guard is simulated.
    """

    def __init__(self, estate: SandboxEstate):
        self.estate = estate

    # -------------------------------------------------------------- reads

    def _work_item(self, work_item_id: str):
        from alm_core.models import WorkItem
        from alm_core.oslc import F_NEW_USERS, users_from_workitem

        raw = self.estate.work_items.get(work_item_id)
        if raw is None:
            return None
        row = {"dcterms:title": raw["summary"], F_NEW_USERS: raw["new_users"]}
        users, _rejected = users_from_workitem(row, work_item_id)
        return WorkItem(work_item_id=work_item_id, summary=raw["summary"],
                        state="inProgress", justification=raw["justification"],
                        new_users_raw=raw["new_users"], users=users)

    async def fetch_open_requests(self, ctx, limit):
        return [self._work_item(w) for w in sorted(self.estate.work_items)][:limit]

    async def fetch_work_item(self, ctx, work_item_id):
        return self._work_item(str(work_item_id).strip())

    async def existing_comments(self, ctx, work_item_id):
        return list(self.estate.comments.get(work_item_id, []))

    async def classify_user(self, ctx, user):
        from alm_core.models import UserState, UserStatus
        from alm_core.tools.jts import _risk

        person = self.estate.people.get(user.userid)
        if person is None or not person.in_ldap:
            state = UserState.MISSING
        elif person.contributor and person.archived:
            state = UserState.ARCHIVED
        elif person.contributor:
            state = UserState.EXISTS
        else:
            state = UserState.READY
        email = person.email if person and person.in_ldap else ""
        # The production risk rules, unchanged.
        risk, reasons = _risk(user, state, email)
        return UserStatus(
            userid=user.userid, state=state,
            ldap_name=person.name if person and person.in_ldap else "",
            ldap_email=email, valid_in_ldap=state not in (UserState.MISSING,),
            has_role=self.estate.has_role(user.userid), role=self.estate.role,
            risk=risk, risk_reasons=reasons)

    async def check_role(self, ctx, userid):
        return self.estate.has_role(userid)

    async def capture_profiles(self, ctx, userids, out_dir):
        from alm_core.tools.evidence import require_valid

        os.makedirs(out_dir, exist_ok=True)
        artifacts: dict[str, str] = {}
        for userid in userids:
            person = self.estate.people.get(userid)
            # Like the real capture: no profile page, no screenshot.
            if not (person and person.contributor and not person.archived):
                continue
            path = os.path.join(out_dir, f"{userid}.png")
            with open(path, "wb") as handle:
                handle.write(_profile_png(userid))
            artifacts[userid] = path
        require_valid(artifacts)  # the real gate: size and duplicate checks
        return artifacts

    # ------------------------------------------------------------- writes

    async def provision_user(self, ctx, user, status):
        from alm_core.models import Operation, Outcome, ProvisionResult, UserState
        from alm_core.tools.base import guarded_write, record

        work_item_id = user.work_item_ids[0] if user.work_item_ids else ""
        person = self.estate.people.get(user.userid)

        if status.state == UserState.EXISTS:
            result = ProvisionResult(
                userid=user.userid, operation=Operation.JTS_CREATE,
                outcome=Outcome.SKIPPED, work_item_id=work_item_id,
                message="already an active JTS contributor")
            await record(ctx, result, "jts_provision")
            return result
        if status.blocked or person is None:
            result = ProvisionResult(
                userid=user.userid, operation=Operation.JTS_CREATE,
                outcome=Outcome.FAILED, work_item_id=work_item_id,
                message=f"cannot provision: LDAP state is {status.state.value}")
            await record(ctx, result, "jts_provision")
            return result

        if status.state == UserState.ARCHIVED:
            async def unarchive():
                person.archived = False
                return True, "reactivated and confirmed active", {}

            return await guarded_write(
                ctx, userid=user.userid, work_item_id=work_item_id,
                operation=Operation.JTS_UNARCHIVE, step="jts_provision",
                action=unarchive)

        async def create():
            person.contributor = True
            person.archived = False
            return True, "created and confirmed active", {"email": person.email}

        return await guarded_write(
            ctx, userid=user.userid, work_item_id=work_item_id,
            operation=Operation.JTS_CREATE, step="jts_provision", action=create)

    async def request_group_membership(self, ctx, user, *, group, domain):
        from alm_core.models import Operation
        from alm_core.tools.base import guarded_write

        async def submit():
            # The sandbox's "Windows worker" applies the change at once, so
            # the verifier can confirm it in the same run.
            self.estate.ad_requests.append(user.userid)
            person = self.estate.people.get(user.userid)
            if person and self.estate.role not in person.roles:
                person.roles.append(self.estate.role)
            return True, f"queued {domain}\\{user.userid} for {group}", {"group": group}

        return await guarded_write(
            ctx, userid=user.userid,
            work_item_id=user.work_item_ids[0] if user.work_item_ids else "",
            operation=Operation.AD_GROUP_ADD, step="ad_request", action=submit)

    async def post_comment(self, ctx, *, work_item_id, userid, text, marker):
        from alm_core.models import Operation
        from alm_core.tools.base import guarded_write

        async def post():
            from alm_core.tools.ewm import normalize_comment

            existing = self.estate.comments.setdefault(work_item_id, [])
            needle = normalize_comment(marker or text)
            if any(needle in normalize_comment(c) for c in existing):
                return True, "an identical comment is already present", {"duplicate": True}
            existing.append(text)
            return True, "comment posted", {"chars": len(text)}

        from alm_core.tools.ewm import comment_fingerprint

        return await guarded_write(
            ctx, userid=userid, work_item_id=work_item_id,
            operation=Operation.WORKITEM_COMMENT, step="workitem_comment",
            variant=comment_fingerprint(text), action=post)

    async def attach_evidence(self, ctx, *, work_item_id, userid, path, filename):
        from alm_core.models import Operation
        from alm_core.tools.base import guarded_write

        async def attach():
            names = self.estate.attachments.setdefault(work_item_id, [])
            if filename in names:
                return True, "already attached", {"duplicate": True}
            names.append(filename)
            return True, "attached", {"filename": filename}

        return await guarded_write(
            ctx, userid=userid, work_item_id=work_item_id,
            operation=Operation.WORKITEM_ATTACH, step="workitem_attach", action=attach)


def _profile_png(userid: str, width: int = 240, height: int = 80) -> bytes:
    """A real, viewable PNG that is unique per user and above the size floor."""
    rng = random.Random(userid)  # noqa: S311 - pseudo-random generation for deterministic test mock PNG image bytes
    base = [rng.randrange(40, 200) for _ in range(3)]
    rows = bytearray()
    for _y in range(height):
        rows.append(0)
        for _x in range(width):
            rows.extend(min(255, c + rng.randrange(0, 56)) for c in base)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ settings

KEY_ORIGIN: dict[str, str] = {}


def load_env() -> None:
    """Load .env into the process environment. Values are never printed.

    A variable already in the Windows environment normally wins over .env. The
    exception is an API key whose Windows value is template text such as
    ``{YOUR_API_KEY}`` - left behind by a tutorial, it would silently shadow
    the real key in .env and every model call would fail as "key not valid".
    """
    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        return
    from alm_core.credentials import API_KEY_ENV_VARS, looks_like_placeholder

    for candidate in (Path.cwd() / ".env", ROOT / ".env"):
        if not candidate.exists():
            continue
        from_file = dotenv_values(candidate)
        preset = {name: os.environ.get(name) for name in API_KEY_ENV_VARS}
        load_dotenv(candidate, override=False)
        for name in API_KEY_ENV_VARS:
            file_value, env_value = from_file.get(name), preset[name]
            if env_value is None:
                KEY_ORIGIN[name] = ".env" if file_value else ""
                continue
            KEY_ORIGIN[name] = "Windows environment"
            if file_value and looks_like_placeholder(env_value) \
                    and not looks_like_placeholder(file_value):
                os.environ[name] = file_value
                KEY_ORIGIN[name] = ".env"
                print(f"[warn] {name} in your Windows environment is a placeholder, not a "
                      "key, so the value from .env is used. Remove it for good with:\n"
                      f"       [Environment]::SetEnvironmentVariable('{name}', $null, 'User')"
                      "\n       then open a new PowerShell window.", flush=True)
        return


def build_settings(*, shadow: bool, model: str = "", rpm: float = 0.0,
                   orchestration: str = "agentic"):
    """Sandbox settings: in-memory, TEST, and never production."""
    from alm_core.config import Settings

    overrides: dict = {"environment": "TEST", "orchestration": orchestration,
                       "llm_enabled": True, "postgres_dsn": "",
                       # Validation insists on a ledger DSN outside shadow mode;
                       # the sandbox's ledger is in memory, so shadow is switched
                       # off after validation below.
                       "shadow_mode": True, "tls_insecure": False}
    if model:
        overrides["agent_model"] = model
        overrides["supervisor_model"] = ""
    if rpm:
        overrides["llm_requests_per_minute"] = rpm
    settings = Settings(**overrides)
    return settings.model_copy(update={"shadow_mode": shadow})


# ----------------------------------------------------------------------- run

async def run_sandbox(settings, *, llm, supervisor_llm=None, auto_approve: bool = False,
                      console: Console | None = None, estate: SandboxEstate | None = None,
                      decide=None, shots_dir: str = "", work_item_ids: list[str] | None = None,
                      operator_request: str = "", thread_id: str = "", control=None,
                      trace=None) -> dict:
    """One complete agentic run against the simulated estate.

    ``decide(payload) -> ApprovalDecision`` overrides the terminal prompt; the
    tests use it. ``control`` is the run's stop switch and ``trace`` records
    every call (the web console passes both). Returns a report of what happened.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from alm_core.store.memory import MemoryStore as LedgerStore
    from alm_core.tools.base import ToolContext

    from .agentic import AgenticRuntime, build_agentic_graph
    from .graph import checkpoint_serde
    from .memory import MemoryStore as AgentMemory

    console = console or Console()
    estate = estate or SandboxEstate.default(settings.jazz_role)
    store = LedgerStore()
    ctx = ToolContext(settings=settings, client=None, store=store, run_id="")
    notifications: list = []

    async def notifier(request):
        notifications.append(request.plan_hash)

    backend = SandboxBackend(estate)
    on_event = console
    if trace is not None:
        from .trace import TracedBackend

        backend = TracedBackend(backend, source="simulated")

        def on_event(kind, data):
            trace.event(kind, data)
            console(kind, data)

    runtime = AgenticRuntime(
        ctx, llm=llm, supervisor_llm=supervisor_llm or llm, memory=AgentMemory(None),
        max_hops=settings.max_hops, notifier=notifier, backend=backend,
        on_event=on_event, shots_dir=shots_dir or str(OUT_DIR / "evidence"),
        control=control)
    graph = build_agentic_graph(runtime, checkpointer=MemorySaver(serde=checkpoint_serde()))

    def terminal(payload):
        return ask_for_decision(payload, auto=auto_approve, console=console)

    answer = decide or terminal

    def decide_traced(payload):
        if trace is not None:
            trace.write({"service": "approval", "kind": "card", "reason": payload.get("reason"),
                         "users": [i.get("userid") for i in payload.get("items", [])]})
        decision = answer(payload)
        if trace is not None:
            trace.write({"service": "approval", "kind": "decision",
                         "approved": decision.approved, "approver": decision.approver,
                         "userids": list(decision.approved_userids or [])})
        return decision

    report = await drive(graph, ctx, thread_id=thread_id or f"sandbox-{uuid.uuid4().hex[:8]}",
                         decide=decide_traced, console=console, trigger="sandbox",
                         work_item_ids=list(work_item_ids or []),
                         operator_request=operator_request)
    report.update(
        policy=runtime.policy.summary(),
        notifications_sent=len(notifications),
        estate={
            "active_with_role": sorted(u for u in estate.people if estate.has_role(u)),
            "comments": estate.comments,
            "attachments": estate.attachments,
            "ad_requests": estate.ad_requests,
        })
    return report


def print_report(report: dict, console: Console) -> None:
    runner_print_report(report, console)
    estate = report["estate"]
    console.line("")
    console.line(f"estate afterwards - active with role: "
                 f"{', '.join(estate['active_with_role']) or '(none)'}")
    for work_item, comments in estate["comments"].items():
        for comment in comments:
            console.line(f"  comment on {work_item}:")
            console.wrap(comment, "    | ")
    for work_item, names in estate["attachments"].items():
        if names:
            console.line(f"  attachments on {work_item}: {', '.join(names)}")


def save_report(report: dict) -> Path:
    return runner_save_report(report, OUT_DIR)


# --------------------------------------------------------------------- check

def check(settings, console: Console,
          ready_hint: str = "python src/agent_sandbox.py") -> int:
    """Prove the key, the model and function calling work - one model request."""
    from . import llm as llm_module

    console.line(f"provider      {settings.llm_provider}")
    console.line(f"agent model   {settings.agent_model}")
    console.line(f"routing model {settings.routing_model}")
    console.line(f"rate limit    {settings.llm_requests_per_minute:g} requests/minute")

    if settings.llm_provider in ("gemini_api", "vertex_express"):
        from alm_core.credentials import GEMINI_KEY_ENV_VARS, gemini_api_key
        from alm_core.errors import CredentialError

        source = next((v for v in GEMINI_KEY_ENV_VARS if os.getenv(v)), "")
        try:
            key = gemini_api_key(settings)
        except CredentialError as err:
            console.line(f"FAIL  {err.message}")
            return 2
        origin = KEY_ORIGIN.get(source, "") if source else ""
        where = f"{source} from {origin}" if origin else (source or "secret provider")
        console.line(f"key           found ({where}); not displayed")
    if settings.llm_provider == "vertex_express":
        for model in dict.fromkeys([settings.agent_model, settings.routing_model]):
            if not _vertex_express_available(key, model, console):
                return 2
    elif settings.llm_provider == "gemini_api":
        available = _list_models(key, console, probe_model=settings.agent_model)
        if available is None:
            return 2
        for model in {settings.agent_model, settings.routing_model}:
            if model in available:
                console.line(f"OK    {model} is available to this key")
            else:
                flash = sorted(m for m in available if "flash" in m)[-6:]
                console.line(f"FAIL  {model} is not available to this key. "
                             f"Try one of: {', '.join(flash) or sorted(available)[:6]}")
                console.line("      (set ALM_AGENT_MODEL in .env, or pass --model)")
                return 2

    if _check_typesafe(settings, console) is False:
        return 2

    client = llm_module.get_agent_llm(settings)
    if client is None:
        console.line("FAIL  no model client could be built - see the warning above")
        return 2
    return asyncio.run(_tool_call_probe(client, console, ready_hint))


def _check_typesafe(settings, console: Console) -> bool | None:
    """True if TypeSafe answers, False if it is configured but broken, None if absent.

    Worth failing on: with ALM_EXTRACTION_PROVIDER=auto a broken key would not
    stop anything - it would quietly hand user-ID recovery back to Gemini.
    """
    from . import llm as llm_module

    key = llm_module._typesafe_key(settings)
    if key is None:
        console.line(f"typesafe      not configured - user-ID recovery uses "
                     f"{'nothing' if settings.extraction_provider == 'typesafe' else 'Gemini'}")
        return None
    try:
        from typesafe_sdk import TypeSafeClient

        with TypeSafeClient(api_key=key, timeout=20.0) as client:
            client.models.list()
    except ImportError:
        console.line("FAIL  TYPESAFE_API_KEY is set but typesafe-sdk is not installed")
        return False
    except Exception as err:  # noqa: BLE001 - reported, not raised
        from alm_core.logging import scrub_secrets

        console.line(f"FAIL  TypeSafe refused the request: "
                     f"{scrub_secrets(f'{type(err).__name__}: {err}')[:200]}")
        return False
    console.line(f"OK    TypeSafe key accepted; user-ID recovery uses {settings.typesafe_model}")
    return True


VERTEX_EXPRESS_URL = ("https://aiplatform.googleapis.com/v1/publishers/google/models/"
                      "{model}:generateContent")


def _google_error(response) -> tuple[str, list[str]]:
    """(message, reasons) from a Google API error body. Never includes the key."""
    from alm_core.logging import scrub_secrets

    try:
        error = response.json().get("error", {})
    except ValueError:
        return scrub_secrets(response.text[:200]), []
    reasons = [d.get("reason", "") for d in error.get("details", [])
               if isinstance(d, dict) and d.get("reason")]
    return scrub_secrets(error.get("message", ""))[:200], reasons


def network_advice(err: Exception) -> str:
    """The one thing to change when a request to Google got no answer.

    "Set HTTPS_PROXY" is the wrong advice when a proxy is already in the path
    and is re-signing HTTPS: then the fix is the company's CA bundle.
    """
    import requests

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if isinstance(err, requests.exceptions.SSLError):
        return ("the certificate Google presented is not trusted here - usually a "
                "corporate proxy inspecting HTTPS. Point REQUESTS_CA_BUNDLE and "
                "SSL_CERT_FILE in .env at the company root CA bundle (.pem)")
    if isinstance(err, requests.exceptions.ProxyError):
        return (f"the proxy {'in HTTPS_PROXY ' if proxy else ''}refused the connection - "
                "check its host:port, and whether it needs a user name and password")
    if isinstance(err, requests.exceptions.Timeout):
        return ("no answer in time - traffic to the internet is being dropped. "
                + ("Check that HTTPS_PROXY is right." if proxy else
                   "Behind a corporate proxy? Set HTTPS_PROXY in .env."))
    if proxy:
        return "cannot connect through HTTPS_PROXY - check that it is right"
    return "cannot connect. Behind a corporate proxy? Set HTTPS_PROXY in .env"


def _vertex_express_call(key: str, model: str):
    """One tiny generateContent on Vertex AI with an API key. Returns the response."""
    import requests

    return requests.post(
        VERTEX_EXPRESS_URL.format(model=model), headers={"x-goog-api-key": key},
        json={"contents": [{"role": "user", "parts": [{"text": "Reply OK."}]}],
              "generationConfig": {"maxOutputTokens": 16}},
        timeout=30)


def _vertex_express_available(key: str, model: str, console: Console) -> bool:
    import requests

    try:
        response = _vertex_express_call(key, model)
    except requests.RequestException as err:
        console.line(f"FAIL  cannot reach Vertex AI ({type(err).__name__}): "
                     f"{network_advice(err)}")
        return False
    if response.status_code == 200:
        console.line(f"OK    {model} answers on Vertex AI with this key")
        return True
    message, reasons = _google_error(response)
    if response.status_code == 404:
        console.line(f"FAIL  {model} is not available on Vertex AI for this key: {message}")
        console.line("      (set ALM_AGENT_MODEL in .env, or pass --model)")
    else:
        console.line(f"FAIL  Vertex AI refused the key (HTTP {response.status_code}): {message}")
        if "CREDENTIALS_MISSING" in reasons or response.status_code == 401:
            console.line("      This is not a Vertex AI API key. If it came from Google AI "
                         "Studio, set ALM_LLM_PROVIDER=gemini_api.")
    return False


def _explain_rejected_key(key: str, model: str, reasons: list[str], console: Console) -> None:
    """Turn Google's refusal into the one thing to change."""
    if not {"API_KEY_INVALID", "SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED"} & set(reasons):
        return
    # Keys made in the Google Cloud console for Vertex AI look like AI Studio
    # keys but are refused by the Gemini Developer API - as unknown, as blocked
    # by the key's API restrictions, or because the project never enabled that
    # API. Whichever refusal it is, ask Vertex AI before blaming the key.
    try:
        vertex_ok = _vertex_express_call(key, model).status_code == 200
    except Exception:  # noqa: BLE001 - a diagnosis must not crash the check
        vertex_ok = False
    if vertex_ok:
        console.line("      This key works on Vertex AI: it is a Google Cloud (Vertex AI) key, "
                     "not a Gemini Developer API key.")
        console.line("      Fix: add  ALM_LLM_PROVIDER=vertex_express  to .env, then run "
                     "--check again.")
    elif "API_KEY_INVALID" in reasons:
        console.line("      Google does not recognise this key on either Gemini "
                     "endpoint. Most likely:")
        console.line("       - it was copied incompletely (AIza... is 39 characters; "
                     "the newer AQ.... format is longer)")
        console.line("       - an older GEMINI_API_KEY in your Windows environment "
                     "overrides .env")
        console.line("       - it was deleted or regenerated in the console")
    elif "SERVICE_DISABLED" in reasons:
        console.line("      The key's Google Cloud project has the Generative Language API "
                     "switched off. Enable it: console.cloud.google.com > APIs & Services "
                     "> Library > \"Generative Language API\".")
    else:
        console.line("      The key has API restrictions that exclude the Generative "
                     "Language API, and Vertex AI refused it too. Either edit the key in "
                     "APIs & Services > Credentials and allow \"Generative Language "
                     "API\", or create a key at https://aistudio.google.com/apikey "
                     "(AI Studio keys are made for this).")


def _list_models(key: str, console: Console, probe_model: str = "") -> set[str] | None:
    import requests

    try:
        # The key goes in a header, never the URL: URLs end up in proxy logs.
        response = requests.get(MODELS_URL, headers={"x-goog-api-key": key},
                                params={"pageSize": 1000}, timeout=20)
    except requests.RequestException as err:
        console.line(f"FAIL  cannot reach the Gemini API ({type(err).__name__}): "
                     f"{network_advice(err)}")
        return None
    if response.status_code != 200:
        detail, reasons = _google_error(response)
        console.line(f"FAIL  the Gemini API refused the key (HTTP {response.status_code}): "
                     f"{detail}")
        if probe_model:
            _explain_rejected_key(key, probe_model, reasons, console)
        return None
    models = response.json().get("models", [])
    return {m["name"].split("/", 1)[-1] for m in models
            if "generateContent" in (m.get("supportedGenerationMethods") or [])}


async def _tool_call_probe(client, console: Console, ready_hint: str = "") -> int:
    from langchain_core.messages import HumanMessage

    tool = {"type": "function", "function": {
        "name": "record_status",
        "description": "Record the status of a system check.",
        "parameters": {"type": "object",
                       "properties": {"status": {"type": "string",
                                                 "description": "Exactly 'ready'."}},
                       "required": ["status"]}}}
    try:
        reply = await client.bind_tools([tool]).ainvoke(
            [HumanMessage(content="Call record_status with status 'ready'.")])
    except Exception as err:  # noqa: BLE001 - reported, not raised
        from alm_core.logging import scrub_secrets

        console.line(f"FAIL  model call failed: {scrub_secrets(str(err))[:300]}")
        return 2
    calls = getattr(reply, "tool_calls", None) or []
    if calls and calls[0].get("name") == "record_status":
        console.line(f"OK    function calling works: {calls[0].get('args')}")
        if ready_hint:
            console.line("")
            console.line(f"Ready. Run the agents with:  {ready_hint}")
        return 0
    console.line("FAIL  the model answered without calling the tool - choose a model "
                 "that supports function calling")
    return 2


# ---------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent_sandbox",
        description="Run the ALM multi-agent system with Gemini against a simulated estate.")
    parser.add_argument("--check", action="store_true",
                        help="verify the key, the model and function calling, then exit")
    parser.add_argument("--auto-approve", action="store_true",
                        help="approve at the gate without asking")
    parser.add_argument("--shadow", action="store_true",
                        help="shadow mode: agents plan, every write is a recorded no-op")
    parser.add_argument("--model", default="",
                        help="override ALM_AGENT_MODEL for this run (e.g. gemini-3.5-flash)")
    parser.add_argument("--rpm", type=float, default=0.0,
                        help="override ALM_LLM_REQUESTS_PER_MINUTE (free tier: keep low)")
    parser.add_argument("--verbose", action="store_true",
                        help="print full tool observations and the service logs")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env()
    # Before any alm_core import configures logging: the terminal is for the run.
    from alm_core.logging import route_console

    # The run's trace records everything; the terminal shows errors unless --verbose.
    route_console("INFO" if args.verbose else "ERROR")
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    console = Console(verbose=args.verbose)
    try:
        settings = build_settings(shadow=args.shadow, model=args.model, rpm=args.rpm)
    except Exception as err:  # noqa: BLE001 - configuration errors, shown plainly
        console.line(f"configuration error: {err}")
        return 2

    if args.check:
        return check(settings, console)

    from . import llm as llm_module

    agent_llm = llm_module.get_agent_llm(settings)
    if agent_llm is None:
        console.line("No model client. Run with --check to see why.")
        return 2

    console.line(f"ALM agent sandbox - {settings.llm_provider}:{settings.agent_model}, "
                 f"{'SHADOW (no writes)' if settings.shadow_mode else 'simulated writes'}, "
                 f"<= {settings.llm_requests_per_minute:g} model calls/minute")
    console.line("The estate is simulated; the agents, policy, approval gate and write "
                 "guard are the production code.")

    report = asyncio.run(run_sandbox(
        settings, llm=agent_llm, supervisor_llm=llm_module.get_supervisor_llm(settings),
        auto_approve=args.auto_approve, console=console))
    print_report(report, console)
    path = save_report(report)
    console.line("")
    console.line(f"full report and audit trail: {path}")
    return 1 if report["halted"] and "rejected" not in report["halt_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
