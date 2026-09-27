"""Evaluate the agents: fixed scenarios and recorded TEST runs, replayed offline.

A prompt, roster or model change is otherwise verified only by a hand-run dry
run, so a regression reaches a live work item before anyone notices. This
module replays scenarios against the simulated estate - real model, production
agents, policy, ledger and tools - and grades the *outcome*, never the wording:

* **Invariants**, checked on every scenario: evidence only on the work items
  that requested the user, comment lines only for requested users, no "added"
  claim for someone who already had an account, no duplicate account, and
  nothing written in a dry run.
* **Expectations** per scenario: who ends up active, which screenshots land
  where, what each comment must and must not say, and - for a recorded run -
  the same approval card and the same writes as the real run produced.

Recorded scenarios come from ``agent_local.py --record`` on the client network:
the real OSLC/LDAP reads plus the run's approval card and writes, saved under
``out/evals/recorded/`` (gitignored; they hold requester names). Replaying them
needs no VPN, only the model.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .runner import Console
from .sandbox import Person, SandboxEstate

ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "out" / "evals"
RECORDED_DIR = EVAL_DIR / "recorded"
_LINE_RE = re.compile(r"^([A-Za-z]{1,3}[0-9][0-9A-Za-z]{3,8}):", re.MULTILINE)


class SilentConsole(Console):
    """The run's step-by-step output, discarded: an eval prints only its verdicts."""

    def line(self, text: str = "") -> None:
        return None


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Scenario:
    name: str
    description: str
    estate: Callable[[], SandboxEstate]
    shadow: bool = False
    # Explicit expectations; each key is optional.
    #   active_after: set of user IDs active with the role afterwards
    #   attachments: {work_item: set of user IDs whose screenshot is attached}
    #   comment_contains / comment_forbids: {work_item: [substrings]}
    #   new_comments: {work_item: exact number of comments this run adds}
    #   approval_items: {(userid, state)} on the card(s)       (recorded runs)
    #   writes: {(userid, operation)} that ended "ok"           (recorded runs)
    #   comment_lines: {work_item: [user lines]}                (recorded runs)
    expect: dict = field(default_factory=dict)
    source: str = "built-in"


# ------------------------------------------------------------ built-in cases

def _estate(people: list[Person], work_items: dict, comments: dict | None = None,
            role: str = "JazzUsers") -> SandboxEstate:
    comments = comments or {}
    return SandboxEstate(people={p.userid: p for p in people}, work_items=work_items,
                         comments={k: list(comments.get(k, [])) for k in work_items},
                         attachments={k: [] for k in work_items}, role=role)


def _present(role: str = "JazzUsers") -> list[Person]:
    return [Person("EF11111", "Bao Nguyen", "bao.nguyen@example.com",
                   contributor=True, roles=[role]),
            Person("JK33333", "Jana Kral", "jana.kral@example.com",
                   contributor=True, roles=[role])]


def _present_items() -> dict:
    return {"2001": {
        "summary": "ALM access - test bench team",
        "justification": "Existing engineers moving to the test bench.",
        "new_users": ("NGUYEN,BAO,bao.nguyen@example.com,EF11111;"
                      "KRAL,JANA,jana.kral@example.com,JK33333;")}}


def _already_reported() -> SandboxEstate:
    from .nodes.closure import render_comment

    earlier = render_comment([("EF11111", "BAO NGUYEN", "already_active"),
                              ("JK33333", "JANA KRAL", "already_active")])
    return _estate(_present(), _present_items(), comments={"2001": [earlier]})


BUILT_IN: list[Scenario] = [
    Scenario(
        "standard",
        "Two work items: a new user, an archived account, an existing user, one "
        "missing from LDAP, and one named only in free text.",
        SandboxEstate.default,
        expect={
            "active_after": {"AB12345", "CD67890", "EF11111", "TB22322"},
            "attachments": {"1001": {"AB12345", "CD67890"},
                            "1002": {"EF11111", "TB22322"}},
            "comment_contains": {"1001": ["AB12345", "User added to JTS", "CD67890",
                                          "User reactivated in JTS"],
                                 "1002": ["EF11111", "User already present in JTS"]},
            "comment_forbids": {"1002": ["GH22222"]},
        }),
    Scenario(
        "dry_run",
        "The standard estate in shadow mode: the agents plan, nothing is written.",
        SandboxEstate.default, shadow=True,
        expect={"active_after": {"EF11111"},
                "attachments": {"1001": set(), "1002": set()},
                "new_comments": {"1001": 0, "1002": 0}}),
    Scenario(
        "all_present",
        "Every requested user already has access: no account is created, and the "
        "comment says so.",
        lambda: _estate(_present(), _present_items()),
        expect={"active_after": {"EF11111", "JK33333"},
                "attachments": {"2001": {"EF11111", "JK33333"}},
                "comment_contains": {"2001": ["User already present in JTS"]},
                "comment_forbids": {"2001": ["User added to JTS"]},
                "new_comments": {"2001": 1}}),
    Scenario(
        "not_in_ldap",
        "The only requested user is missing from LDAP: nothing may be written.",
        lambda: _estate(
            [Person("GH22222", "", "", in_ldap=False)],
            {"3001": {"summary": "ALM access - contractor",
                      "justification": "Short contract.",
                      "new_users": "GHOST,USER,ghost.user@example.com,GH22222;"}}),
        expect={"active_after": set(), "attachments": {"3001": set()},
                "new_comments": {"3001": 0}}),
    Scenario(
        "already_reported",
        "The same outcome is already on the work item (an earlier run, or the "
        "CLI): no second comment may be posted.",
        _already_reported,
        expect={"active_after": {"EF11111", "JK33333"}, "new_comments": {"2001": 0}}),
]


# ------------------------------------------------------------ recorded runs

def load_recorded(path: Path) -> Scenario:
    """A scenario file written by ``agent_local.py --record``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data["estate"]
    people = [Person(**p) for p in raw["people"]]
    estate_json = {"people": people, "work_items": raw["work_items"],
                   "comments": raw.get("comments") or {}, "role": raw.get("role", "JazzUsers")}

    def build() -> SandboxEstate:
        return _estate(copy.deepcopy(estate_json["people"]), estate_json["work_items"],
                       estate_json["comments"], estate_json["role"])

    base = data.get("baseline") or {}
    expect: dict = {}
    if base.get("approval_items"):
        expect["approval_items"] = {(i["userid"], i["state"]) for i in base["approval_items"]}
    if base.get("writes"):
        expect["writes"] = {(w["userid"], w["operation"]) for w in base["writes"]
                            if w.get("outcome") == "ok"}
    if base.get("comment_lines"):
        expect["comment_lines"] = {k: sorted(v) for k, v in base["comment_lines"].items()}
    return Scenario(name=path.stem, description=data.get("description", "recorded TEST run"),
                    estate=build, shadow=bool(data.get("dry_run")), expect=expect,
                    source=f"recorded {data.get('recorded_at', '')}".strip())


class RecordingBackend:
    """Wraps the live backend and keeps every read a run makes.

    Only reads are recorded; writes pass straight through. ``save()`` writes a
    scenario file that ``load_recorded`` replays against the simulated estate.
    """

    def __init__(self, inner, role: str):
        self.inner = inner
        self.role = role
        self.work_items: dict[str, dict] = {}
        self.people: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = {}
        self.posted: dict[str, list[str]] = {}
        self.approval_items: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _keep_item(self, item) -> None:
        self.work_items[item.work_item_id] = {
            "summary": item.summary, "justification": item.justification,
            "new_users": item.new_users_raw}

    async def fetch_open_requests(self, ctx, limit):
        items = await self.inner.fetch_open_requests(ctx, limit)
        for item in items:
            self._keep_item(item)
        return items

    async def fetch_work_item(self, ctx, work_item_id):
        item = await self.inner.fetch_work_item(ctx, work_item_id)
        if item is not None:
            self._keep_item(item)
        return item

    async def classify_user(self, ctx, user):
        from alm_core.models import UserState

        status = await self.inner.classify_user(ctx, user)
        self.people[user.userid] = {
            "userid": user.userid, "name": status.ldap_name, "email": status.ldap_email,
            "in_ldap": bool(status.valid_in_ldap),
            "contributor": status.state in (UserState.EXISTS, UserState.ARCHIVED),
            "archived": status.state == UserState.ARCHIVED,
            "roles": [self.role] if status.has_role else []}
        return status

    async def existing_comments(self, ctx, work_item_id):
        comments = await self.inner.existing_comments(ctx, work_item_id)
        if comments is not None:
            self.comments[work_item_id] = list(comments)
        return comments

    async def post_comment(self, ctx, *, work_item_id, userid, text, marker):
        result = await self.inner.post_comment(ctx, work_item_id=work_item_id,
                                               userid=userid, text=text, marker=marker)
        if getattr(result.outcome, "value", result.outcome) == "ok" and not result.replayed:
            self.posted.setdefault(work_item_id, []).append(text)
        return result

    def note_approval(self, payload: dict) -> None:
        for item in payload.get("items", []):
            self.approval_items.append({"userid": item.get("userid"),
                                        "state": item.get("state"),
                                        "action": item.get("action")})

    def save(self, report: dict, *, dry_run: bool, directory: Path = RECORDED_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        writes = [{"userid": r.get("userid"), "operation": r.get("operation"),
                   "outcome": r.get("outcome"), "work_item_id": r.get("work_item_id")}
                  for r in report.get("results") or []]
        comment_lines = {wi: sorted(ln for text in texts for ln in _user_lines(text))
                         for wi, texts in self.posted.items()}
        stamp = datetime.now(timezone.utc)
        path = directory / f"run-{stamp:%Y%m%dT%H%M%S}-{report.get('thread_id', 'x')}.json"
        path.write_text(json.dumps({
            "description": f"recorded TEST run {report.get('thread_id', '')}",
            "recorded_at": stamp.isoformat(), "dry_run": dry_run,
            "estate": {"role": self.role, "people": list(self.people.values()),
                       "work_items": self.work_items, "comments": self.comments},
            "baseline": {"approval_items": self.approval_items, "writes": writes,
                         "comment_lines": comment_lines},
        }, indent=2), encoding="utf-8")
        return path


# ------------------------------------------------------------------ grading

def _user_lines(comment: str) -> list[str]:
    return [line.strip() for line in comment.splitlines() if _LINE_RE.match(line.strip())]


def _requested(estate: SandboxEstate) -> dict[str, set[str]]:
    """Every user ID written anywhere in each work item's request."""
    from .llm import userid_candidates

    return {wi: set(userid_candidates(f"{raw.get('new_users', '')} "
                                      f"{raw.get('justification', '')}"))
            for wi, raw in estate.work_items.items()}


def grade(scenario: Scenario, before: SandboxEstate, after: SandboxEstate,
          report: dict, cards: list[dict]) -> list[Check]:
    checks: list[Check] = []
    requested = _requested(before)
    had_account = {u for u, p in before.people.items() if p.contributor}
    new_comments = {wi: after.comments.get(wi, [])[len(before.comments.get(wi, [])):]
                    for wi in after.work_items}

    checks.append(Check("run completed", not report.get("halted"),
                        report.get("halt_reason", "")))

    stray = {wi: sorted({Path(n).stem.upper() for n in names} - requested.get(wi, set()))
             for wi, names in after.attachments.items()}
    stray = {wi: users for wi, users in stray.items() if users}
    checks.append(Check("evidence only on the requesting work item", not stray,
                        f"stray: {stray}" if stray else ""))

    foreign = {}
    false_added = []
    for wi, comments in new_comments.items():
        for line in (ln for c in comments for ln in _user_lines(c)):
            userid = _LINE_RE.match(line).group(1).upper()
            if userid not in requested.get(wi, set()):
                foreign.setdefault(wi, []).append(userid)
            if userid in had_account and "User added to JTS" in line:
                false_added.append(userid)
    checks.append(Check("comments name only requested users", not foreign,
                        f"foreign: {foreign}" if foreign else ""))
    checks.append(Check("no 'added' claim for an existing account", not false_added,
                        ", ".join(false_added)))

    duplicates = [r.get("userid") for r in report.get("results") or []
                  if r.get("operation") == "jts_create" and r.get("outcome") == "ok"
                  and r.get("userid") in had_account]
    checks.append(Check("no duplicate account created", not duplicates, ", ".join(duplicates)))

    if scenario.shadow:
        wrote = [f"{r.get('userid')}:{r.get('operation')}" for r in report.get("results") or []
                 if r.get("outcome") == "ok"]
        changed = any(new_comments.values()) or any(after.attachments.values()) \
            or bool(after.ad_requests)
        checks.append(Check("dry run wrote nothing", not wrote and not changed,
                            ", ".join(wrote)))

    expect = scenario.expect
    active = {u for u in after.people if after.has_role(u)}
    if "active_after" in expect:
        want = set(expect["active_after"])
        checks.append(Check("active users afterwards", active == want,
                            f"got {sorted(active)}, want {sorted(want)}"))
    for wi, want in (expect.get("attachments") or {}).items():
        got = {Path(n).stem.upper() for n in after.attachments.get(wi, [])}
        checks.append(Check(f"screenshots on {wi}", got == set(want),
                            f"got {sorted(got)}, want {sorted(want)}"))
    for wi, needles in (expect.get("comment_contains") or {}).items():
        text = "\n".join(new_comments.get(wi, []))
        missing = [n for n in needles if n not in text]
        checks.append(Check(f"comment on {wi} says what happened", not missing,
                            f"missing: {missing}" if missing else ""))
    for wi, needles in (expect.get("comment_forbids") or {}).items():
        text = "\n".join(new_comments.get(wi, []))
        present = [n for n in needles if n in text]
        checks.append(Check(f"comment on {wi} claims nothing false", not present,
                            f"present: {present}" if present else ""))
    for wi, count in (expect.get("new_comments") or {}).items():
        got = len(new_comments.get(wi, []))
        checks.append(Check(f"comments posted on {wi}", got == count,
                            f"got {got}, want {count}"))
    if "approval_items" in expect:
        got = {(i.get("userid"), i.get("state")) for c in cards for i in c.get("items", [])}
        want = set(expect["approval_items"])
        checks.append(Check("same approval card as the recorded run", got == want,
                            f"only replay: {sorted(got - want)}; only recorded: "
                            f"{sorted(want - got)}" if got != want else ""))
    if "writes" in expect:
        got = {(r.get("userid"), r.get("operation")) for r in report.get("results") or []
               if r.get("outcome") == "ok"}
        want = set(expect["writes"])
        checks.append(Check("same writes as the recorded run", got == want,
                            f"only replay: {sorted(got - want)}; only recorded: "
                            f"{sorted(want - got)}" if got != want else ""))
    for wi, want in (expect.get("comment_lines") or {}).items():
        got = sorted(ln for c in new_comments.get(wi, []) for ln in _user_lines(c))
        checks.append(Check(f"same comment lines on {wi}", got == list(want),
                            f"got {got}, want {list(want)}" if got != list(want) else ""))
    return checks


# ------------------------------------------------------------------ running

async def run_scenario(scenario: Scenario, settings, *, llm, supervisor_llm=None,
                       console: Console | None = None, shots_dir: str = "") -> dict:
    """Run one scenario end to end and grade it. Approval is automatic."""
    from alm_core.models import ApprovalDecision

    from .sandbox import run_sandbox

    before = scenario.estate()
    estate = copy.deepcopy(before)
    cards: list[dict] = []

    def decide(payload: dict) -> ApprovalDecision:
        cards.append(payload)
        shown = [str(i.get("userid", "")) for i in payload.get("items", [])]
        return ApprovalDecision(thread_id=payload.get("thread_id", ""), approved=True,
                                approver="eval:auto", plan_hash=payload.get("plan_hash", ""),
                                approved_userids=shown)

    run_settings = settings.model_copy(update={"shadow_mode": scenario.shadow})
    started = time.monotonic()
    report = await run_sandbox(run_settings, llm=llm, supervisor_llm=supervisor_llm,
                               console=console or Console(), estate=estate, decide=decide,
                               shots_dir=shots_dir or str(EVAL_DIR / "evidence" / scenario.name))
    checks = grade(scenario, before, estate, report, cards)
    return {"scenario": scenario.name, "source": scenario.source,
            "passed": all(c.passed for c in checks),
            "checks": [c.__dict__ for c in checks],
            "metrics": report.get("metrics") or {},
            "wall_seconds": round(time.monotonic() - started, 1)}


def select(names: list[str], recorded: list[Path]) -> list[Scenario]:
    by_name = {s.name: s for s in BUILT_IN}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}. "
                         f"Built-in: {', '.join(by_name)}")
    chosen = [by_name[n] for n in names] if names else ([] if recorded else list(BUILT_IN))
    return chosen + [load_recorded(p) for p in recorded]


def _recorded_paths(value: str) -> list[Path]:
    path = Path(value)
    if path.is_dir():
        return sorted(path.glob("*.json"))
    if path.is_file():
        return [path]
    raise SystemExit(f"--recorded: {value} is not a file or folder")


def print_summary(results: list[dict], console: Console) -> None:
    console.line("")
    for result in results:
        mark = "PASS" if result["passed"] else "FAIL"
        m = result["metrics"]
        console.line(f"{mark}  {result['scenario']:18} {m.get('model_calls', '?'):>3} model "
                     f"calls, {m.get('tool_calls', '?'):>3} tool calls, "
                     f"{result['wall_seconds']:>6.1f}s   ({result['source']})")
        for check in result["checks"]:
            if not check["passed"]:
                console.line(f"      x {check['name']}: {check['detail']}")
    passed = sum(r["passed"] for r in results)
    console.line("")
    console.line(f"{passed}/{len(results)} scenario(s) passed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent_eval",
        description="Replay scenarios against the simulated estate and grade the outcomes.")
    parser.add_argument("--scenario", action="append", default=[], metavar="NAME",
                        help=f"built-in scenario to run (repeatable): "
                             f"{', '.join(s.name for s in BUILT_IN)}. Default: all")
    parser.add_argument("--recorded", metavar="PATH", default="",
                        help=f"a recorded run, or a folder of them (e.g. {RECORDED_DIR})")
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument("--orchestration", choices=["guided", "agentic"], default="guided")
    parser.add_argument("--model", default="", help="override ALM_AGENT_MODEL")
    parser.add_argument("--rpm", type=float, default=0.0,
                        help="override ALM_LLM_REQUESTS_PER_MINUTE")
    parser.add_argument("--verbose", action="store_true", help="show every agent step")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from . import llm as llm_module
    from .sandbox import build_settings, load_env

    load_env()
    os.environ["ALM_LOG_LEVEL"] = "INFO" if args.verbose else "ERROR"
    logging.getLogger().setLevel(logging.INFO if args.verbose else logging.ERROR)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    console = Console(verbose=args.verbose)

    recorded = _recorded_paths(args.recorded) if args.recorded else []
    scenarios = select(args.scenario, recorded)
    if args.list:
        for s in scenarios:
            console.line(f"{s.name:18} {s.source:10} {s.description}")
        return 0

    settings = build_settings(shadow=False, model=args.model, rpm=args.rpm,
                              orchestration=args.orchestration)
    agent_llm = llm_module.get_agent_llm(settings)
    if agent_llm is None:
        console.line("No model client. Run python src/agent_sandbox.py --check to see why.")
        return 2
    quiet = console if args.verbose else SilentConsole()

    console.line(f"ALM agent eval - {settings.llm_provider}:{settings.agent_model}, "
                 f"{args.orchestration}, {len(scenarios)} scenario(s)")
    results = []
    for scenario in scenarios:
        console.line(f"running {scenario.name} ...")
        results.append(asyncio.run(run_scenario(
            scenario, settings, llm=agent_llm,
            supervisor_llm=llm_module.get_supervisor_llm(settings), console=quiet)))
    print_summary(results, console)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    path = EVAL_DIR / f"eval-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps({"model": settings.agent_model,
                                "orchestration": args.orchestration,
                                "results": results}, indent=2), encoding="utf-8")
    console.line(f"results: {path}")
    return 0 if all(r["passed"] for r in results) else 1
