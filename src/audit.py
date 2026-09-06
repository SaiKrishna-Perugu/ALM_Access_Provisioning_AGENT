"""Per-user audit trail shared by every pipeline step.

Each step buffers records in memory and flushes them to
``out/audit/<run_id>.<step>.json``. Because run_pipeline.py runs most steps as
subprocesses, the run id travels through the ALM_RUN_ID environment variable so
every child writes into the same run; the orchestrator then merges the per-step
files into ``out/audit/run-<run_id>.json`` and prints the summary table.

Statuses:
  ok            - the step did the thing and confirmed it
  skipped       - deliberately not done (dry run, --no-unarchive, already done)
  not_attempted - the step never reached this user (an earlier user aborted it)
  timeout       - waited for a condition that never became true
  failed        - attempted and did not succeed

``not_attempted`` exists because the audit used to record one user's error
message against users the run never touched, which made the forensics actively
misleading. A summary must never present unperformed work as success either, so
the table reports attempted / succeeded / skipped / not attempted / failed
separately instead of collapsing them into SUCCESS and FAILED.
"""
from __future__ import annotations

import json
import os
import time
import traceback

import alm_config

AUDIT_DIR = "out/audit"
STEP_ORDER = ["retrieve", "gpt", "import", "verify", "comment", "attach"]

OK = "ok"
SKIPPED = "skipped"
NOT_ATTEMPTED = "not_attempted"
TIMEOUT = "timeout"
FAILED = "failed"

# Worst-wins ranking when a user has several records for the same step.
_SEVERITY = {OK: 0, SKIPPED: 1, NOT_ATTEMPTED: 2, TIMEOUT: 3, FAILED: 4}

_records: list[dict] = []


def run_id() -> str:
    """Shared id for this pipeline run, created once and inherited by child steps."""
    rid = os.getenv("ALM_RUN_ID")
    if not rid:
        rid = time.strftime("%Y%m%dT%H%M%S")
        os.environ["ALM_RUN_ID"] = rid
    return rid


def record(step: str, userid: str, status: str, outcome: str = "",
           message: str = "", exc: BaseException | None = None, **extra) -> dict:
    """Buffer one per-user outcome. Pass exc inside an except block to capture the traceback."""
    rec = {
        "schema": alm_config.AUDIT_SCHEMA,
        "run_id": run_id(),
        "env": alm_config.alm_env(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "step": step,
        "userid": userid,
        "status": status,
        "outcome": outcome,
        "message": message,
    }
    if exc is not None:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        # format_exception (not format_exc) so callers may pass a stored exception.
        rec["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))
    rec.update(extra)
    _records.append(rec)
    return rec


def record_not_attempted(step: str, userids, reason: str) -> None:
    """Mark users the step never reached, so they are not confused with failures."""
    for uid in userids:
        record(step, uid, NOT_ATTEMPTED, outcome="not_attempted", message=reason)


def flush(step: str) -> str | None:
    """Write this process's records for a step and clear the buffer."""
    rows = [r for r in _records if r["step"] == step]
    if not rows:
        return None
    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(AUDIT_DIR, f"{run_id()}.{step}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    _records[:] = [r for r in _records if r["step"] != step]
    return path


def load_all(rid: str = "") -> list[dict]:
    """Every record written for a run, across all steps and processes."""
    rid = rid or run_id()
    if not os.path.isdir(AUDIT_DIR):
        return []
    rows: list[dict] = []
    for name in sorted(os.listdir(AUDIT_DIR)):
        if not (name.startswith(f"{rid}.") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(AUDIT_DIR, name), encoding="utf-8") as fh:
                rows.extend(json.load(fh))
        except (OSError, ValueError):
            continue
    return rows


def outcomes_for_step(step: str, rid: str = "") -> dict[str, dict]:
    """{userid: worst record} for one step of a run.

    The comment step uses this to describe what the import actually did, instead
    of asserting the same sentence for everyone.
    """
    best: dict[str, dict] = {}
    for rec in load_all(rid):
        if rec.get("step") != step:
            continue
        uid = rec.get("userid") or ""
        if not uid:
            continue
        current = best.get(uid)
        if current is None or _SEVERITY.get(rec.get("status", OK), 0) >= _SEVERITY.get(
                current.get("status", OK), 0):
            best[uid] = rec
    return best


def aggregate(rid: str = "", extra: dict | None = None) -> tuple[str, list[dict]]:
    """Merge the per-step files into out/audit/run-<id>.json. Returns (path, records)."""
    rid = rid or run_id()
    rows = load_all(rid)
    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(AUDIT_DIR, f"run-{rid}.json")
    payload = {
        "schema": alm_config.AUDIT_SCHEMA,
        "run_id": rid,
        "env": alm_config.alm_env(),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(rows),
        "records": rows,
    }
    payload.update(extra or {})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return path, rows


def _worst(a: str, b: str) -> str:
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b


def by_user(records: list[dict]) -> dict[str, dict[str, str]]:
    """{userid: {step: worst status}} across all records."""
    grid: dict[str, dict[str, str]] = {}
    for r in records:
        uid = r.get("userid") or ""
        if not uid:
            continue
        cell = grid.setdefault(uid, {})
        step = r.get("step", "")
        cell[step] = _worst(cell.get(step, OK), r.get("status", OK))
    return grid


def failures(records: list[dict]) -> list[dict]:
    """Records that did not succeed, in step order.

    ``not_attempted`` is excluded: it is a consequence of another failure, not an
    independent one, and counting it would inflate every aborted run.
    """
    bad = [r for r in records if r.get("status") in (FAILED, TIMEOUT)]
    return sorted(bad, key=lambda r: (STEP_ORDER.index(r["step"])
                                      if r.get("step") in STEP_ORDER else 99,
                                      r.get("userid", "")))


_CELL = {OK: "OK", FAILED: "FAIL", SKIPPED: "skip", NOT_ATTEMPTED: "n/a", TIMEOUT: "TIMEOUT"}


def counts(records: list[dict]) -> dict[str, int]:
    """Per-user tallies by worst status across every step of the run."""
    tally = {"users": 0, "succeeded": 0, "skipped": 0, "not_attempted": 0,
             "timed_out": 0, "failed": 0}
    for cells in by_user(records).values():
        worst = OK
        for status in cells.values():
            worst = _worst(worst, status)
        tally["users"] += 1
        tally[{OK: "succeeded", SKIPPED: "skipped", NOT_ATTEMPTED: "not_attempted",
               TIMEOUT: "timed_out", FAILED: "failed"}[worst]] += 1
    return tally


def summary_table(records: list[dict], dry_run: bool = False) -> str:
    """Human-readable per-user matrix plus the per-status ID lists.

    ``dry_run=True`` suppresses the word SUCCESS entirely - a dry run performed
    no work and must never read as though it did.
    """
    grid = by_user(records)
    if not grid:
        return "No audit records for this run."

    steps = [s for s in STEP_ORDER if any(s in cells for cells in grid.values())]
    widths = {s: max(len(s), 7) for s in steps}
    uid_w = max(8, max(len(u) for u in grid))

    out = ["  " + "USERID".ljust(uid_w) + "  "
           + "  ".join(s.upper().ljust(widths[s]) for s in steps) + "  RESULT"]
    buckets: dict[str, list[str]] = {OK: [], SKIPPED: [], NOT_ATTEMPTED: [],
                                     TIMEOUT: [], FAILED: []}
    for uid in sorted(grid):
        cells = grid[uid]
        worst = OK
        for status in cells.values():
            worst = _worst(worst, status)
        buckets[worst].append(uid)
        row = "  ".join(_CELL.get(cells.get(s, ""), "-").ljust(widths[s]) for s in steps)
        out.append(f"  {uid.ljust(uid_w)}  {row}  {_CELL.get(worst, worst)}")

    out.append("")
    if dry_run:
        out.append(f"PLANNED ({len(grid)}): {', '.join(sorted(grid))}")
        out.append("DRY RUN - nothing was written; no user was provisioned.")
    else:
        for label, key in (("SUCCEEDED", OK), ("SKIPPED", SKIPPED),
                           ("NOT ATTEMPTED", NOT_ATTEMPTED), ("TIMED OUT", TIMEOUT),
                           ("FAILED", FAILED)):
            ids = buckets[key]
            if ids or key in (OK, FAILED):
                out.append(f"{label:<14}({len(ids)}): {', '.join(ids) if ids else '-'}")

    bad = failures(records)
    if bad:
        out.append("")
        out.append("Failure detail:")
        for r in bad:
            detail = r.get("error") or r.get("message") or r.get("outcome") or "-"
            out.append(f"  {r.get('userid', '?')} @ {r.get('step', '?')} "
                       f"[{r.get('status')}] {r.get('timestamp', '')}: {detail}")
    return "\n".join(out)
