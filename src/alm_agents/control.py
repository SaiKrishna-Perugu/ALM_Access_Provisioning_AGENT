"""Stopping a run: from the web console, from Ctrl+C, or from another terminal.

A stop is cooperative and safe. The run finishes the tool call it is in - a
write is never cut off halfway, where nobody could say whether it happened -
then halts with the reason, writes its report and audit, and ends. A model call
in progress is abandoned at once: it writes nothing.

Three ways in:

* the web console's Stop button (:meth:`RunControl.request_stop`);
* Ctrl+C in the terminal running ``agent_local.py`` (a second Ctrl+C aborts);
* ``python src/agent_local.py --stop [THREAD_ID]`` from any other terminal on
  this machine, which writes a stop file every running local or web run reads.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import os
import threading
import time
from pathlib import Path

STOP_FILE_NAME = "STOP"


def stop_file_for(ledger_path: str) -> Path:
    """The stop file shared by every local run using this ledger's folder."""
    return Path(ledger_path).parent / STOP_FILE_NAME


def write_stop_file(path: Path, *, thread_id: str = "", by: str = "") -> dict:
    """Ask the running run (``thread_id``), or every run, to stop."""
    request = {"thread_id": thread_id or "all", "by": by or f"cli:{getpass.getuser()}",
               "at": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request), encoding="utf-8")
    return request


class RunControl:
    """The stop switch one run checks between steps."""

    def __init__(self, *, thread_id: str = "", stop_file: Path | None = None):
        self.thread_id = thread_id
        self.stop_file = stop_file
        self.started = time.time()
        self._event = threading.Event()
        self.by = ""
        self.reason = ""
        self._last_poll = 0.0

    def request_stop(self, by: str, reason: str = "") -> None:
        if not self._event.is_set():
            self.by = by
            self.reason = reason or f"stopped by {by}"
            self._event.set()

    def stop_requested(self) -> bool:
        if self._event.is_set():
            return True
        self._poll_file()
        return self._event.is_set()

    def _poll_file(self) -> None:
        """A stop file newer than this run, for this run or for all runs."""
        if self.stop_file is None:
            return
        now = time.monotonic()
        if now - self._last_poll < 0.5:
            return  # a stat per step is cheap, but not in a tight loop
        self._last_poll = now
        try:
            # A cheap pre-check for a file left over from an earlier run. The
            # slack covers coarse file timestamps; the "at" inside decides.
            if os.path.getmtime(self.stop_file) < self.started - 2:
                return
            request = json.loads(self.stop_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        target = str(request.get("thread_id") or "all")
        if float(request.get("at") or 0) >= self.started and target in ("all", self.thread_id):
            self.request_stop(str(request.get("by") or "cli"))

    async def wait(self, interval: float = 0.25) -> None:
        """Return once a stop is requested - to race against a slow model call."""
        while not self.stop_requested():
            await asyncio.sleep(interval)


class StopRequested(Exception):
    """The operator stopped the run while a model call was in flight."""


async def unless_stopped(control: RunControl | None, awaitable):
    """Await a model call, abandoning it the moment the run is stopped.

    Only for calls that write nothing - model calls. A tool call is never raced:
    a write must finish and be recorded, or nobody knows whether it happened.
    """
    task = asyncio.ensure_future(awaitable)
    if control is None:
        return await task
    watch = asyncio.ensure_future(control.wait())
    try:
        done, _ = await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        raise
    finally:
        watch.cancel()
    if task in done:
        return task.result()
    task.cancel()
    raise StopRequested()
