"""A run's trace: every call to every service, as structured records.

The code that talks to a service - the HTTP session, the ledger, GPT, the
model - calls :func:`emit`. Nothing happens unless a run has installed a sink
with :func:`set_sink`; the local runner and the web console install one that
writes a JSON-lines file per run. One sink per process, because one run at a
time is what the local tools allow, and HTTP calls run on worker threads that
a context variable would not reach.

What is never traced: request and response bodies (a login posts the
password), headers (session cookies) and query values whose name looks like a
secret. Records pass through :func:`alm_core.logging.scrub_secrets` as well.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_sink: Callable[[dict], None] | None = None
_lock = threading.Lock()
_SECRET_PARAM = ("pass", "secret", "token", "key", "auth", "cookie", "session")


def set_sink(sink: Callable[[dict], None] | None) -> None:
    """Install (or, with ``None``, remove) the process's trace sink."""
    global _sink
    with _lock:
        _sink = sink


def active() -> bool:
    return _sink is not None


def emit(service: str, kind: str, **fields) -> None:
    """Record one event. Never raises: tracing must not break a run."""
    sink = _sink
    if sink is None:
        return
    try:
        sink({"service": service, "kind": kind, **fields})
    except Exception:  # noqa: S110, BLE001 - a broken sink must not stop the work
        pass


@contextmanager
def span(service: str, kind: str, **fields):
    """Time a block and record it once, with ``ok`` and any error.

    The block may add fields to the yielded dict (a status, a count).
    """
    extra: dict = {}
    started = time.perf_counter()
    try:
        yield extra
    except BaseException as err:
        emit(service, kind, ok=False, ms=_ms(started),
             error=f"{type(err).__name__}: {err}"[:500], **fields, **extra)
        raise
    emit(service, kind, ok=extra.pop("ok", True), ms=_ms(started), **fields, **extra)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def safe_url(url: str) -> str:
    """The URL with secret-looking query values blanked."""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query = [(k, "[redacted]" if any(s in k.lower() for s in _SECRET_PARAM) else v)
                 for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit(parts._replace(query=urlencode(query, safe=":/?=&,")))
    except ValueError:
        return url.split("?", 1)[0]


def http_response(response, *_args, **_kwargs):
    """A ``requests`` response hook: one record per HTTP exchange.

    ``requests`` calls it for every response, each redirect included, so the
    redirect chain of a form login shows up hop by hop.
    """
    if _sink is None:
        return response
    elapsed = getattr(response, "elapsed", None)
    emit("http", "request",
         method=getattr(response.request, "method", ""),
         url=safe_url(response.url or ""),
         status=response.status_code,
         ms=int(elapsed.total_seconds() * 1000) if elapsed else 0,
         bytes=int(response.headers.get("Content-Length") or 0),
         content_type=response.headers.get("Content-Type", "").split(";")[0])
    return response
