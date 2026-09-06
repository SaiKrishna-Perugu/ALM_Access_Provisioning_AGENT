"""Structured run log alongside the human-readable output.

Every step used to communicate only through ``print()``, so capturing a run
meant piping it through ``Tee-Object`` and the result was unparseable. This
module keeps the operator-facing text exactly as it was and additionally
appends one JSON object per event to ``out/logs/<run_id>.jsonl``.

Use :func:`say` where the code previously called ``print`` for something worth
keeping, and :func:`event` for machine-only detail. Both are safe to call from
child processes: the run id is inherited through ``ALM_RUN_ID`` so the whole
pipeline lands in one file.
"""
from __future__ import annotations

import json
import os
import sys
import time

import audit

LOG_DIR = "out/logs"

# Field names whose values are never written to the log, whatever the caller
# passes. Cheap insurance against a password reaching disk through a log call.
_REDACT = {"password", "j_password", "secret", "token", "credential", "pwd"}


def _path() -> str:
    return os.path.join(LOG_DIR, f"{audit.run_id()}.jsonl")


def _clean(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        out[key] = "***" if key.lower() in _REDACT else value
    return out


def event(name: str, level: str = "info", **fields) -> None:
    """Append one structured record. Never raises - logging must not break a run."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": audit.run_id(),
        "level": level,
        "event": name,
        "pid": os.getpid(),
    }
    record.update(_clean(fields))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def say(message: str, name: str = "message", level: str = "info", **fields) -> None:
    """Print for the operator and record the same thing structurally."""
    print(message, flush=True)
    event(name, level=level, message=message, **fields)


def warn(message: str, name: str = "warning", **fields) -> None:
    print(message, file=sys.stderr, flush=True)
    event(name, level="warning", message=message, **fields)
