"""Shapes shared by every store for the run registry and the job queue.

Postgres hands back JSONB as Python objects and timestamps as datetimes;
SQLite hands back text. These helpers turn either into the same dicts, so the
API, the worker and the tests see one shape whichever store is underneath.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# The run registry's columns a caller may set. Anything else is refused, which
# also keeps column names out of reach of input when an UPDATE is built.
RUN_COLUMNS = ("run_id", "status", "mode", "scope", "requested_by", "trigger",
               "operator_request", "environment", "version", "error", "report")
RUN_JSON = ("scope", "report")
RUN_SELECT = ("thread_id",) + RUN_COLUMNS + ("created_at", "updated_at")

JOB_SELECT = ("id", "kind", "thread_id", "payload", "status", "attempts", "available_at",
              "locked_by", "locked_until", "error", "created_at", "finished_at")

RUN_STATUSES = ("queued", "running", "awaiting_approval", "stopping", "done", "stopped",
                "failed")


def log_audit(log, event) -> None:
    """One structured log line per audit row, ``audit=true``: what a SIEM
    subscribes to (a log sink filtered on it), whichever store holds the row."""
    log.info("audit", audit=True, run_id=event.run_id, step=event.step,
             userid=event.userid, work_item=event.work_item_id,
             operation=getattr(event.operation, "value", event.operation) or "",
             outcome=getattr(event.outcome, "value", event.outcome),
             approver=event.approver or "", environment=event.environment,
             message=event.message)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def run_values(fields: dict, *, json_as_text: bool = True) -> dict:
    """Validate the fields of a run update; JSON columns serialised as text."""
    unknown = set(fields) - set(RUN_COLUMNS)
    if unknown:
        raise ValueError(f"unknown run field(s): {', '.join(sorted(unknown))}")
    if "status" in fields and fields["status"] not in RUN_STATUSES:
        raise ValueError(f"unknown run status {fields['status']!r}")
    values = {}
    for column in RUN_COLUMNS:
        if column in fields:
            value = fields[column]
            if column in RUN_JSON and json_as_text:
                value = _dumps(value)
            values[column] = value
    return values


def _loads(value):
    if value is None or isinstance(value, dict | list):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _stamp(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else (value or "")


def run_row(row) -> dict:
    run = dict(zip(RUN_SELECT, row, strict=True))
    for column in RUN_JSON:
        run[column] = _loads(run[column])
    run["created_at"] = _stamp(run["created_at"])
    run["updated_at"] = _stamp(run["updated_at"])
    return run


def job_row(row) -> dict:
    job = dict(zip(JOB_SELECT, row, strict=True))
    job["payload"] = _loads(job["payload"]) or {}
    for column in ("available_at", "locked_until", "created_at", "finished_at"):
        job[column] = _stamp(job[column])
    return job


def next_job_status(ok: bool, attempts: int, max_attempts: int) -> str:
    """Where a finished job goes: done, back to the queue, or dead."""
    if ok:
        return "done"
    return "queued" if attempts < max_attempts else "dead"
