"""Drive one agentic run from a terminal: print it live, pause for approval, report.

Shared by the sandbox (simulated estate) and the local runner (the real one), so
the two cannot drift: what you see and approve in the sandbox is exactly what
you will see and approve against EWM and JTS.

Plain ASCII output throughout - Windows consoles vary.
"""
from __future__ import annotations

import getpass
import json
import textwrap
import time
from pathlib import Path


class Console:
    """Prints the run as it happens. Also the ``on_event`` hook of the runtime."""

    def __init__(self, verbose: bool = False, width: int = 100):
        self.verbose = verbose
        self.width = width

    def line(self, text: str = "") -> None:
        print(text, flush=True)

    def wrap(self, text: str, indent: str) -> None:
        for chunk in textwrap.wrap(" ".join(str(text).split()), self.width - len(indent)):
            self.line(indent + chunk)

    def __call__(self, kind: str, data: dict) -> None:
        if kind == "supervisor":
            tag = (" (fixed order)" if data.get("guided")
                   else " (fallback)" if data.get("fallback") else "")
            self.line("")
            self.line(f"[hop {data.get('hop')}] supervisor -> {data.get('next')}{tag}")
            if data.get("why"):
                self.wrap(f"why: {data['why']}", "    ")
            if data.get("task") and data.get("next") != "DONE":
                self.wrap(f"task: {data['task']}", "    ")
        elif kind == "agent_text" and (self.verbose or len(data.get("text", "")) < 400):
            self.wrap(f"{data.get('agent')} says: {data.get('text', '')[:400]}", "    ")
        elif kind == "tool_call":
            args = ", ".join(f"{k}={_short(v)}" for k, v in (data.get("args") or {}).items())
            flag = "DENIED " if data.get("denied") else ""
            self.line(f"    {data.get('agent')}: {flag}{data.get('tool')}({args})")
            observation = str(data.get("observation", ""))
            limit = 1200 if self.verbose else 160
            self.wrap(_digest(observation)[:limit], "        | ")
        elif kind == "model_error":
            self.line(f"    {data.get('agent')}: MODEL ERROR {data.get('error')}")


def _digest(observation: str) -> str:
    """One readable line from an observation, which is often indented JSON."""
    try:
        return json.dumps(json.loads(observation), separators=(", ", ": "), default=str)
    except ValueError:
        return " ".join(observation.split())


def _short(value) -> str:
    text = json.dumps(value, default=str) if not isinstance(value, str) else repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ------------------------------------------------------------------ approval

# True while the terminal waits at the y/N prompt. Ctrl+C there ends the run at
# once: nothing is in flight at the gate, so there is nothing to finish first.
PROMPTING = False


def ask_for_decision(payload: dict, *, auto: bool, console: Console,
                     approver_prefix: str = "sandbox",
                     auto_note: str = "auto-approved (--auto-approve)"):
    """Show the batch and read y/N. The approver recorded is the OS user."""
    from alm_core.models import ApprovalDecision

    console.line("")
    console.line("=" * 72)
    console.line("APPROVAL REQUIRED - the run is paused (LangGraph interrupt)")
    if payload.get("reason"):
        console.wrap(f"reason: {payload['reason']}", "  ")
    for item in payload.get("items", []):
        reasons = "; ".join(item.get("risk_reasons") or []) or "-"
        work_items = ",".join(item.get("work_item_ids") or []) or "-"
        console.line(f"  {item.get('userid'):9} {item.get('action') or item.get('state'):12}"
                     f" risk={item.get('risk'):6} wi={work_items:10} {reasons}")
    console.line("=" * 72)

    if auto:
        approved, approver, comment = True, f"{approver_prefix}:auto-approve", auto_note
        console.line(auto_note)
    else:
        global PROMPTING
        PROMPTING = True
        try:
            answer = input("Approve this batch? [y/N] ").strip().lower()
        except EOFError:
            answer = ""  # no terminal to answer from: not approved
        finally:
            PROMPTING = False
        approved = answer in {"y", "yes"}
        approver = f"{approver_prefix}:{getpass.getuser()}"
        comment = "approved at the terminal" if approved else "rejected at the terminal"
    # The decision covers exactly the users the human was shown - never
    # "everyone", which would include users added to the run afterwards.
    shown = [str(item.get("userid", "")) for item in payload.get("items", [])
             if item.get("userid")]
    return ApprovalDecision(thread_id=payload.get("thread_id", ""), approved=approved,
                            approver=approver, plan_hash=payload.get("plan_hash", ""),
                            comment=comment, approved_userids=shown)


async def pending_interrupt(graph, thread_id: str) -> dict | None:
    """The approval payload if the run is paused at the gate, else None."""
    from .graph import run_config

    snapshot = await graph.aget_state(run_config(thread_id))
    for task in snapshot.tasks or ():
        for item in getattr(task, "interrupts", ()) or ():
            return item.value
    for item in getattr(snapshot, "interrupts", ()) or ():
        return item.value
    return None


# ----------------------------------------------------------------------- run

MAX_APPROVAL_ROUNDS = 3


class RunModeMismatch(Exception):
    """A resume asked for a different mode than the run started in."""

    def __init__(self, thread_id: str, recorded: str, requested: str):
        self.thread_id, self.recorded, self.requested = thread_id, recorded, requested
        flag = " --commit" if recorded == "commit" else ""
        super().__init__(
            f"run {thread_id} was started as a {recorded or 'run of unknown mode'}, "
            f"but this resume is a {requested}. "
            + (f"Resume it the same way: --resume {thread_id}{flag}"
               if recorded else "Start a new run instead."))


async def drive(graph, ctx, *, thread_id: str, decide, console: Console,
                resume: bool = False, work_item_ids: list[str] | None = None,
                trigger: str = "manual", operator_request: str = "") -> dict:
    """Start (or resume) a run and see it through every approval pause.

    ``decide(payload) -> ApprovalDecision`` answers each pause. On ``resume`` the
    run continues from its last checkpoint: from the approval prompt if it was
    paused there, or from the last completed step if the process died mid-run.
    """
    from alm_core.logging import bind_run

    from .graph import resume_run, run_config, start_run

    config = run_config(thread_id)
    started = time.monotonic()
    if resume:
        snapshot = await graph.aget_state(config)
        if not snapshot.values:
            raise LookupError(f"no saved run with thread id {thread_id!r}")
        from .graph import run_mode_of

        recorded, requested = snapshot.values.get("run_mode", ""), run_mode_of(ctx)
        # Unknown mode (a run from before modes were recorded) may only be
        # resumed as a dry run: it can never be promoted to writing.
        if recorded != requested and (recorded or requested == "commit"):
            raise RunModeMismatch(thread_id, recorded, requested)
        ctx.run_id = snapshot.values.get("run_id", "")
        ctx.thread_id = thread_id
        bind_run(run_id=ctx.run_id, thread_id=thread_id)
        if snapshot.next and await pending_interrupt(graph, thread_id) is None:
            console.line(f"resuming {thread_id} at {', '.join(snapshot.next)}")
            await graph.ainvoke(None, config=config)
    else:
        await start_run(graph, ctx, thread_id=thread_id, work_item_ids=work_item_ids,
                        trigger=trigger, operator_request=operator_request)

    approvals = 0
    while True:
        payload = await pending_interrupt(graph, thread_id)
        if payload is None:
            break
        approvals += 1
        if approvals > MAX_APPROVAL_ROUNDS:
            console.line(f"stopping: more than {MAX_APPROVAL_ROUNDS} approval rounds in one "
                         f"run. Resume with --resume {thread_id} after reviewing it.")
            break
        await resume_run(graph, ctx, thread_id=thread_id, decision=decide(payload))

    return await build_report(graph, ctx, thread_id=thread_id, approvals=approvals,
                              wall_seconds=time.monotonic() - started)


async def build_report(graph, ctx, *, thread_id: str, approvals: int | None,
                       wall_seconds: float) -> dict:
    """The run's report, from its final checkpoint and its audit trail.

    ``approvals`` is the number of approval rounds this process saw; None
    counts them from the run's own history (a run resumed by many workers).
    """
    from .graph import run_config

    final = (await graph.aget_state(run_config(thread_id))).values
    if approvals is None:
        approvals = sum(1 for e in final.get("agent_history") or []
                        if e.get("agent") == "approval")
    return {
        "run_id": ctx.run_id,
        "thread_id": thread_id,
        "halted": bool(final.get("halted")),
        "halt_reason": final.get("halt_reason", ""),
        "hops": final.get("hops", 0),
        "agents": [e.get("agent") for e in final.get("agent_history") or []],
        "results": [r.model_dump(mode="json") for r in final.get("results") or []],
        "approval_rounds": approvals,
        "metrics": run_metrics(final, wall_seconds),
        "audit_events": await ctx.store.run_events(ctx.run_id),
    }


def run_metrics(state: dict, wall_seconds: float) -> dict:
    """What the run cost: model calls, tool calls, hops, time, per agent.

    Built from the run's own state, so a resumed run reports the whole run -
    except wall time, which is this process's share.
    """
    per_agent: dict[str, dict] = {}
    for entry in state.get("agent_history") or []:
        name = entry.get("agent", "?")
        if name == "approval":
            continue
        row = per_agent.setdefault(name, {"runs": 0, "model_calls": 0,
                                          "tool_calls": 0, "denials": 0})
        row["runs"] += 1
        row["model_calls"] += int(entry.get("model_calls") or 0)
        row["tool_calls"] += int(entry.get("calls") or 0)
        row["denials"] += int(entry.get("denials") or 0)
    agent_calls = sum(r["model_calls"] for r in per_agent.values())
    supervisor_calls = int(state.get("supervisor_model_calls") or 0)
    return {
        "model_calls": agent_calls + supervisor_calls,
        "agent_model_calls": agent_calls,
        "supervisor_model_calls": supervisor_calls,
        "tool_calls": sum(r["tool_calls"] for r in per_agent.values()),
        "hops": int(state.get("hops") or 0),
        "wall_seconds": round(wall_seconds, 1),
        "per_agent": per_agent,
    }


def print_report(report: dict, console: Console) -> None:
    console.line("")
    console.line("=" * 72)
    status = f"HALTED: {report['halt_reason']}" if report["halted"] else "complete"
    console.line(f"run {report['run_id']}  thread {report['thread_id']}  {status}")
    console.line(f"hops {report['hops']}  approvals {report['approval_rounds']}  "
                 f"policy {report.get('policy', {})}")
    metrics = report.get("metrics") or {}
    if metrics:
        console.line(f"cost: {metrics['model_calls']} model calls "
                     f"(agents {metrics['agent_model_calls']}, supervisor "
                     f"{metrics['supervisor_model_calls']}), {metrics['tool_calls']} tool "
                     f"calls, {metrics['wall_seconds']}s")
    console.line("")
    console.line("writes (through guarded_write):")
    for r in report["results"]:
        replay = " (replay)" if r.get("replayed") else ""
        console.line(f"  {r['userid']:9} {r['operation']:17} {r['outcome']:13} "
                     f"{r.get('message', '')[:60]}{replay}")
    if not report["results"]:
        console.line("  (none)")


def save_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"run-{report['run_id'] or 'unknown'}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
