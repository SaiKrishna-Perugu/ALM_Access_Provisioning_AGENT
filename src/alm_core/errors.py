"""Typed exception hierarchy.

The CLI pipeline signalled failure with broad ``except Exception`` and printed
strings, which meant a network blip, a rejected credential and a malformed
server response were indistinguishable to the caller. An autonomous graph has to
branch on *why* something failed - a transient error is retried, an
authentication error pages a human, and a data error routes the work item to
manual review - so the reason has to survive as a type.

``retryable`` is the property the orchestrator actually branches on.
"""
from __future__ import annotations


class AlmError(Exception):
    """Base class. Every error raised by alm_core is one of these."""

    retryable = False

    def __init__(self, message: str, *, context: dict | None = None):
        super().__init__(message)
        self.message = message
        self.context = context or {}

    def as_dict(self) -> dict:
        return {"type": type(self).__name__, "message": self.message,
                "retryable": self.retryable, "context": self.context}


# --------------------------------------------------------------- configuration

class ConfigError(AlmError):
    """Missing or contradictory configuration. Never retryable."""


class CredentialError(AlmError):
    """No usable credential could be obtained from any provider."""


# ------------------------------------------------------------------- transport

class TransportError(AlmError):
    """The request did not complete: DNS, TLS, connection reset, timeout."""

    retryable = True


class RateLimitedError(TransportError):
    """The server asked us to slow down (HTTP 429 / Retry-After)."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ServerError(TransportError):
    """5xx from EWM/JTS. Transient often enough to be worth a bounded retry."""


# --------------------------------------------------------------------- identity

class AuthenticationError(AlmError):
    """Credentials were rejected. Retrying with the same secret cannot help."""


class AuthorizationError(AlmError):
    """Authenticated, but not permitted to read or write this resource."""


# ------------------------------------------------------------------------ data

class DataError(AlmError):
    """The server answered, but the payload was not what the contract requires."""


class ParseError(DataError):
    """A field could not be interpreted - e.g. a malformed New Users entry."""


class NotFoundError(DataError):
    """The work item, contributor or attachment does not exist."""


# ------------------------------------------------------------------ operations

class IdempotencyViolation(AlmError):
    """A write was attempted whose idempotency key is already committed.

    Raised rather than swallowed: it means two runs are racing on the same
    (work item, user, operation), which is a scheduling defect worth surfacing.
    """


class ApprovalRequired(AlmError):
    """A write was attempted without a recorded human approval covering it."""


class ApprovalExpired(AlmError):
    """The approval token is past its expiry, or its plan no longer matches."""


class EvidenceInvalid(AlmError):
    """Evidence artifacts failed validation - e.g. two users share a screenshot."""


class WorkerUnavailable(AlmError):
    """The Windows Kerberos worker did not accept or acknowledge the job."""

    retryable = True
