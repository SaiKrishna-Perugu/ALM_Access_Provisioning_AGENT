"""One run's trace on disk: every model call, tool call, service call, HTTP
exchange, ledger step and log line, one JSON object per line.

    out/local/traces/<thread_id>.jsonl      real runs (agent_local.py, agent_web.py)
    out/sandbox/traces/<thread_id>.jsonl    sandbox runs

A resumed run appends to its thread's file. Read one with
``python src/agent_local.py --trace last`` (add ``--follow`` while it runs), or
in the web console's Trace tab, which can also download it.

The trace holds what the run saw, requesters' names included - it is for the
operator's own machine, and ``--purge-older-than`` deletes it with the other
run data. Secrets never reach it: no request bodies or headers, secret-looking
query values blanked, and every line passed through ``scrub_secrets``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from alm_core import trace as core_trace
from alm_core.logging import scrub_secrets

MAX_FIELD = 20_000   # one observation or model reply, before it is clipped
KEEP_IN_MEMORY = 20_000

# Which service a backend method talks to - for filtering, not for routing.
BACKEND_SERVICE = {
    "fetch_open_requests": "ewm", "fetch_work_item": "ewm", "existing_comments": "ewm",
    "post_comment": "ewm", "attach_evidence": "ewm",
    "classify_user": "jts", "check_role": "jts", "provision_user": "jts",
    "request_group_membership": "gpt", "capture_profiles": "browser",
}
_AGENT_RE = re.compile(r"You are the (\w+) agent\.")
_HTTPX_LINE = re.compile(r'HTTP Request: (\w+) (\S+) "HTTP/[\d.]+ (\d{3})')


def _clip(value, limit: int = MAX_FIELD):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f" ... [{len(value) - limit} more characters]"
    return value


class RunTrace:
    """Writes a run's records to its file, and keeps them for the web console."""

    def __init__(self, path: Path, *, thread_id: str, hosts: dict[str, str] | None = None):
        self.path = path
        self.thread_id = thread_id
        # Known hosts, so an HTTP record says "ewm" or "jts", not just a URL.
        self.hosts = {h.lower(): name for name, h in (hosts or {}).items() if h}
        self.started = time.monotonic()
        self.records: deque[dict] = deque(maxlen=KEEP_IN_MEMORY)
        self._seq = 0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._log_handler: _LogForwarder | None = None
        self._bound = None
        # Called with every record after it is written - a worker mirrors records
        # to the store so any API replica can show them. Must not raise.
        self.listeners: list = []

    # -------------------------------------------------------------- writing

    def write(self, record: dict) -> None:
        record = {k: _clip(v) for k, v in record.items()}
        if record.get("service") == "http" and "system" not in record:
            host = urlsplit(str(record.get("url", ""))).hostname or ""
            record["system"] = self.hosts.get(host.lower(), host)
        with self._lock:
            record = {"seq": self._seq,
                      "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                      "t": round(time.monotonic() - self.started, 3),
                      "thread_id": self.thread_id, **record}
            self._seq += 1
            line = scrub_secrets(json.dumps(record, ensure_ascii=False, default=str))
            if not self._file.closed:  # a late event after the run ended
                self._file.write(line + "\n")
                self._file.flush()
            record = json.loads(line)
            self.records.append(record)
        for listener in list(self.listeners):
            try:
                listener(record)
            except Exception:  # noqa: S110, BLE001 - a listener must not break the trace
                pass

    def event(self, kind: str, data: dict) -> None:
        """The runtime's progress events (supervisor, tool_call, agent_text, ...)."""
        service = {"supervisor": "supervisor", "tool_call": "tool",
                   "agent_text": "agent", "model_error": "model"}.get(kind, "run")
        self.write({"service": service, "kind": kind, **data})

    def since(self, after: int = -1, limit: int = 1000) -> list[dict]:
        with self._lock:
            return [r for r in self.records if r["seq"] > after][:limit]

    # ------------------------------------------------------------ lifecycle

    def __enter__(self) -> RunTrace:
        """Trace the whole process (one run at a time: local and web runs)."""
        core_trace.set_sink(self.write)
        self._log_handler = _LogForwarder(self)
        logging.getLogger().addHandler(self._log_handler)
        return self

    def __exit__(self, *exc) -> None:
        core_trace.set_sink(None)
        self.close()

    @contextmanager
    def bound(self):
        """Trace only the current context - one run's task in a worker that
        drives several runs at once. Use as ``with trace.bound():``."""
        with core_trace.bind_sink(self.write):
            self._log_handler = _LogForwarder(self)
            logging.getLogger().addHandler(self._log_handler)
            try:
                yield self
            finally:
                self._close()

    def _close(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

    def close(self) -> None:
        """Stop listening and close the file. Safe to call twice."""
        self._close()
        with self._lock:
            if not self._file.closed:
                self._file.close()


class _LogForwarder(logging.Handler):
    """Every structured log line of the run, whatever the console shows."""

    def __init__(self, trace: RunTrace):
        super().__init__(level=logging.DEBUG)
        self.trace = trace

    def emit(self, record: logging.LogRecord) -> None:
        # Only the run whose context produced the line: with several runs in
        # one process, each trace keeps its own log lines.
        if core_trace.current_sink() != self.trace.write:
            return
        try:
            message = record.getMessage()
            http = _HTTPX_LINE.match(message) if record.name.startswith("httpx") else None
            if http:
                # The model provider's own HTTP calls (httpx logs one line each).
                method, url, status = http.groups()
                self.trace.write({"service": "http", "kind": "request", "method": method,
                                  "url": core_trace.safe_url(url), "status": int(status),
                                  "system": urlsplit(url).hostname or ""})
                return
            try:
                fields = json.loads(message)
            except ValueError:
                fields = {"event": message}
            if not isinstance(fields, dict):
                fields = {"event": message}
            fields.pop("timestamp", None)
            fields.pop("thread_id", None)
            event = str(fields.pop("event", record.name))
            if record.exc_info:
                fields["exception"] = logging.Formatter().formatException(record.exc_info)
            self.trace.write({"service": "log", "kind": event,
                              "level": record.levelname.lower(),
                              "logger": record.name, **fields})
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


def open_run_trace(folder: Path, thread_id: str, *, settings=None) -> RunTrace:
    hosts = {}
    if settings is not None:
        for name in ("ewm", "jts"):
            server = getattr(settings, f"{name}_server", "") or ""
            hosts[name] = urlsplit(server).hostname or ""
    return RunTrace(Path(folder) / "traces" / f"{thread_id}.jsonl",
                    thread_id=thread_id, hosts=hosts)


# ----------------------------------------------------------------- the model

class TracedModel:
    """A chat model that records every call it makes. Otherwise transparent."""

    def __init__(self, inner, *, role: str = "", tools: list[str] | None = None):
        self._inner = inner
        self._role = role
        self._tools = tools or []

    def bind_tools(self, tools, **kwargs):
        names = [t.get("function", {}).get("name") or t.get("name", "")
                 if isinstance(t, dict) else getattr(t, "name", str(t)) for t in tools]
        return TracedModel(self._inner.bind_tools(tools, **kwargs), role=self._role,
                           tools=names)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _caller(self, messages) -> str:
        first = messages[0] if messages else None
        text = getattr(first, "content", None)
        if text is None and isinstance(first, tuple | list) and len(first) == 2:
            text = first[1]
        found = _AGENT_RE.search(str(text or ""))
        return found.group(1) if found else self._role

    def _record(self, messages, response, started: float, error: str = "") -> None:
        if not core_trace.active():
            return
        last = messages[-1] if messages else None
        last_text = getattr(last, "content", None)
        if last_text is None and isinstance(last, tuple | list) and len(last) == 2:
            last_text = last[1]
        fields = {
            "caller": self._caller(messages),
            "model": str(getattr(self._inner, "model", "")
                         or getattr(self._inner, "model_name", "") or ""),
            "ms": int((time.perf_counter() - started) * 1000),
            "messages": len(messages or []),
            "prompt_chars": sum(len(str(getattr(m, "content", m))) for m in messages or []),
            "input": _clip(str(last_text or ""), 4000),
            "tools_offered": self._tools,
        }
        if error:
            core_trace.emit("model", "call", ok=False, error=error, **fields)
            return
        usage = getattr(response, "usage_metadata", None) or {}
        from .llm import response_text

        core_trace.emit(
            "model", "call", ok=True,
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
            tool_calls=[{"name": c.get("name"), "args": c.get("args")}
                        for c in getattr(response, "tool_calls", None) or []],
            reply=response_text(response), **fields)

    async def ainvoke(self, messages, *args, **kwargs):
        started = time.perf_counter()
        try:
            response = await self._inner.ainvoke(messages, *args, **kwargs)
        except BaseException as err:
            self._record(messages, None, started,
                         error=scrub_secrets(f"{type(err).__name__}: {err}")[:500])
            raise
        self._record(messages, response, started)
        return response

    def invoke(self, messages, *args, **kwargs):
        started = time.perf_counter()
        try:
            response = self._inner.invoke(messages, *args, **kwargs)
        except BaseException as err:
            self._record(messages, None, started,
                         error=scrub_secrets(f"{type(err).__name__}: {err}")[:500])
            raise
        self._record(messages, response, started)
        return response


# --------------------------------------------------------------- the backend

class TracedBackend:
    """Records every call the tools make to EWM, JTS, GPT and the browser."""

    def __init__(self, inner, *, source: str):
        self._inner = inner
        self._source = source  # "live" or "simulated"

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in BACKEND_SERVICE or not callable(attr):
            return attr

        async def call(ctx, *args, **kwargs):
            fields = {"source": self._source, **_describe(args, kwargs)}
            with core_trace.span(BACKEND_SERVICE[name], name, **fields) as extra:
                result = await attr(ctx, *args, **kwargs)
                extra.update(_summarise(result))
            return result

        return call


def _describe(args, kwargs) -> dict:
    out = {}
    for value in list(args) + list(kwargs.values()):
        userid = getattr(value, "userid", None)
        if userid:
            out["userid"] = userid
    for key in ("work_item_id", "userid", "limit", "group", "domain", "filename"):
        if key in kwargs:
            out[key] = kwargs[key]
    if args and isinstance(args[0], str | int) and "work_item_id" not in out:
        out["target"] = args[0]
    if args and isinstance(args[0], list):
        out["userids"] = [str(u) for u in args[0]]
    return out


def _summarise(result) -> dict:
    if result is None:
        return {"result": "none"}
    if isinstance(result, list):
        return {"result": f"{len(result)} item(s)"}
    if isinstance(result, dict):
        return {"result": f"{len(result)} entr(y/ies)"}
    if isinstance(result, bool):
        return {"result": result}
    outcome = getattr(result, "outcome", None)
    if outcome is not None:
        return {"outcome": getattr(outcome, "value", str(outcome)),
                "ok": getattr(outcome, "value", "") == "ok",
                "message": str(getattr(result, "message", ""))[:500],
                "replayed": bool(getattr(result, "replayed", False))}
    state = getattr(result, "state", None)
    if state is not None:
        return {"state": getattr(state, "value", str(state))}
    return {"result": type(result).__name__}


# ------------------------------------------------------------------ reading

def read_trace(path: Path) -> list[dict]:
    records = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def format_record(record: dict) -> str:
    """One line for a terminal: time, offset, service, what happened."""
    service = str(record.get("service", ""))
    kind = str(record.get("kind", ""))
    ms = record.get("ms")
    took = f" {ms / 1000:.1f}s" if isinstance(ms, int | float) and ms >= 100 else (
        f" {ms}ms" if isinstance(ms, int | float) else "")
    flag = "" if record.get("ok", True) else "  FAILED"
    stamp = str(record.get("at", ""))[11:23]
    head = f"{stamp} +{record.get('t', 0):>7.1f}s  {service:10} "
    if service == "model":
        calls = ", ".join(c.get("name", "") for c in record.get("tool_calls") or [])
        tokens = (f" tokens {record.get('input_tokens')}/{record.get('output_tokens')}"
                  if record.get("input_tokens") is not None else "")
        what = f"{record.get('caller', '')} -> {calls or 'text'}{tokens}"
        if record.get("error"):
            what += f"  {record['error']}"
    elif service == "tool":
        args = ", ".join(f"{k}={v}" for k, v in (record.get("args") or {}).items())
        denied = "  DENIED" if record.get("denied") else ""
        what = f"{record.get('agent', '')}.{record.get('tool', '')}({args[:120]}){denied}"
    elif service == "supervisor":
        what = f"hop {record.get('hop')} -> {record.get('next')}  {str(record.get('why', ''))[:120]}"
    elif service == "http":
        what = (f"{record.get('system', '')} {record.get('method', '')} "
                f"{str(record.get('url', ''))[:140]} -> {record.get('status', record.get('error', ''))}")
    elif service == "ledger":
        what = (f"{kind} {record.get('operation', '')} {record.get('userid', '')} "
                f"wi={record.get('work_item_id', '')} {record.get('outcome', '')}")
        kind = ""
    elif service == "log":
        rest = {k: v for k, v in record.items() if k not in (
            "seq", "at", "t", "thread_id", "service", "kind", "level", "logger", "run_id")}
        what = f"[{record.get('level', '')}] {kind} {json.dumps(rest, default=str)[:160]}"
        kind = ""
    else:
        rest = {k: v for k, v in record.items() if k not in (
            "seq", "at", "t", "thread_id", "service", "kind", "ms", "ok")}
        what = json.dumps(rest, default=str)[:180]
    label = f"{kind} " if kind and service not in ("model", "tool", "supervisor", "http") else ""
    return f"{head}{label}{what}{took}{flag}"
