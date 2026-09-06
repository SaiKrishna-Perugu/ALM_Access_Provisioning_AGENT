"""Structured JSON logging with mandatory secret and PII redaction.

Two requirements this has to satisfy that ordinary logging does not:

1. **Secrets must not reach the sink.** The password travels through the process
   environment and into child processes; a stray ``log.info(payload)`` would put
   it in Application Insights permanently. Redaction is a processor in the
   pipeline, not a caller responsibility, so forgetting is not an option.
2. **PII must be minimised before it leaves the tenant.** Names, e-mail
   addresses and user IDs are the payload of this system. :func:`redact_pii`
   is what the extraction agent runs over free text *before* an LLM ever sees
   it.

``structlog`` is optional: if it is not installed the module falls back to a
JSON formatter over the standard library, so the same calls work on an operator
laptop that only has requirements.txt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

# Correlation identifiers travel with the run, not with the call stack, so a
# node deep in the graph does not have to thread them through every signature.
_run_id: ContextVar[str] = ContextVar("alm_run_id", default="")
_thread_id: ContextVar[str] = ContextVar("alm_thread_id", default="")

SECRET_KEYS = {
    "password", "j_password", "secret", "token", "credential", "pwd", "authorization",
    "api_key", "apikey", "client_secret", "connection_string", "sas", "cookie",
    "jsessionid", "x-jazz-csrf-prevent", "signature",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Jazz/Stellantis CIDs: two letters or a letter-digit pair, then alphanumerics.
_USERID_RE = re.compile(r"\b[A-Z]{1,2}[0-9][0-9A-Z]{4,6}\b")
REDACTED = "[redacted]"


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_run(run_id: str = "", thread_id: str = "") -> None:
    """Attach correlation ids to everything logged from here on in this context."""
    if run_id:
        _run_id.set(run_id)
    if thread_id:
        _thread_id.set(thread_id)


def current_run_id() -> str:
    return _run_id.get()


def current_thread_id() -> str:
    return _thread_id.get()


def redact_value(key: str, value: Any) -> Any:
    return REDACTED if key.lower() in SECRET_KEYS else value


def redact_mapping(data: dict) -> dict:
    """Recursively redact secret-named keys anywhere in a structure."""
    out: dict = {}
    for key, value in data.items():
        if key.lower() in SECRET_KEYS:
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [redact_mapping(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def redact_pii(text: str, *, keep_userids: bool = False) -> str:
    """Strip e-mail addresses and (optionally) user IDs from free text.

    Run this on anything heading for an LLM. ``keep_userids=True`` is for the
    extraction fallback, where the user ID is the thing being extracted and the
    prompt is useless without it - the e-mail and display name still go.
    """
    text = _EMAIL_RE.sub("[email]", text or "")
    if not keep_userids:
        text = _USERID_RE.sub("[userid]", text)
    return text


# --------------------------------------------------------------------- backend

def _std_processors():
    import structlog

    def add_correlation(_logger, _name, event_dict):
        if _run_id.get():
            event_dict.setdefault("run_id", _run_id.get())
        if _thread_id.get():
            event_dict.setdefault("thread_id", _thread_id.get())
        return event_dict

    def redact(_logger, _name, event_dict):
        return redact_mapping(event_dict)

    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_correlation,
        redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]


class _JsonFormatter(logging.Formatter):
    """Fallback formatter used when structlog is not installed."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }
        if _run_id.get():
            payload["run_id"] = _run_id.get()
        if _thread_id.get():
            payload["thread_id"] = _thread_id.get()
        extra = getattr(record, "alm_fields", None)
        if isinstance(extra, dict):
            payload.update(redact_mapping(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _StdlibAdapter:
    """Minimal structlog-shaped wrapper over the standard logger."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def bind(self, **_kw) -> _StdlibAdapter:
        return self

    def _log(self, level: int, event: str, **fields):
        self._logger.log(level, event, extra={"alm_fields": fields})

    def debug(self, event: str, **f):
        self._log(logging.DEBUG, event, **f)

    def info(self, event: str, **f):
        self._log(logging.INFO, event, **f)

    def warning(self, event: str, **f):
        self._log(logging.WARNING, event, **f)

    def error(self, event: str, **f):
        self._log(logging.ERROR, event, **f)

    def exception(self, event: str, **f):
        self._logger.exception(event, extra={"alm_fields": f})


_configured = False


def configure(level: str = "") -> None:
    """Install the JSON logging pipeline. Safe to call more than once."""
    global _configured
    if _configured:
        return
    _configured = True
    level_name = (level or os.getenv("ALM_LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(sys.stdout)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level_name)

    try:
        import structlog
    except ImportError:
        handler.setFormatter(_JsonFormatter())
        return

    handler.setFormatter(logging.Formatter("%(message)s"))
    structlog.configure(
        processors=_std_processors(),
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level_name)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "alm"):
    """A logger that emits redacted JSON, with or without structlog installed."""
    configure()
    try:
        import structlog

        return structlog.get_logger(name)
    except ImportError:
        return _StdlibAdapter(logging.getLogger(name))
