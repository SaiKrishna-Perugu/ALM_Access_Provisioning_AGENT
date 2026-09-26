"""The contracts every agent, tool and API endpoint speaks.

These types are the reason the LLM can never write. A model may *propose* a
``RequestedUser`` or draft comment text, but the value only becomes an action
after it has survived validation here, and the tools accept nothing else. A
hallucinated user ID fails ``USERID_RE`` and never reaches LDAP.

Field names deliberately match the JSON the existing CLI already writes
(``out/alm_users.json``), so the two systems can read each other's artifacts
during the migration.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Jazz user IDs on this estate: 1-2 letters, a digit, then alphanumerics
# (SF58083, T0195G3, MWPDOO01). Anything else is not a user ID, whatever
# produced it.
USERID_PATTERN = r"^[A-Za-z]{1,3}[0-9][0-9A-Za-z]{3,8}$"
# An EWM work item id is a plain number. Anything else never reaches a URL or
# an OSLC query: the value can come from a model reading requester-written text.
WORK_ITEM_ID_PATTERN = r"^[0-9]{1,10}$"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Operation(str, Enum):
    """The write operations that need an idempotency key."""

    JTS_CREATE = "jts_create"
    JTS_UNARCHIVE = "jts_unarchive"
    AD_GROUP_ADD = "ad_group_add"
    WORKITEM_COMMENT = "workitem_comment"
    WORKITEM_ATTACH = "workitem_attach"


class UserState(str, Enum):
    """What the registry says about a user, before we do anything."""

    READY = "ready"            # in LDAP, not yet a JTS contributor
    EXISTS = "exists"          # already an active contributor
    ARCHIVED = "archived"      # a contributor, but deactivated
    INVALID = "invalid"        # LDAP entry marked invalid
    MISSING = "missing"        # no LDAP entry at all
    UNKNOWN = "unknown"        # not looked up yet


class Outcome(str, Enum):
    """What a step did. Mirrors the CLI audit vocabulary deliberately."""

    OK = "ok"
    SKIPPED = "skipped"
    NOT_ATTEMPTED = "not_attempted"
    TIMEOUT = "timeout"
    FAILED = "failed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Strict(BaseModel):
    """Base: unknown fields are an error, not something to shrug at.

    A tool call carrying a field nobody defined is either a contract drift or a
    model inventing structure. Both should fail loudly.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)


# --------------------------------------------------------------------- inputs

class SourceWorkItem(Strict):
    """Where a requested user came from."""

    work_item_id: str = Field(min_length=1, max_length=32)
    summary: str = ""
    access_type: str = ""
    domain: str = ""
    work_areas: str = ""


class RequestedUser(Strict):
    """One user a work item asks to have provisioned."""

    userid: str = Field(pattern=USERID_PATTERN)
    email: EmailStr | None = None
    first_name: str = ""
    last_name: str = ""
    source_work_items: list[SourceWorkItem] = Field(default_factory=list)
    # Set when the deterministic parser could not read the row and the LLM
    # fallback produced it. Such a user is always routed to human review.
    extracted_by_llm: bool = False
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("userid")
    @classmethod
    def _canonical(cls, value: str) -> str:
        # JTS user IDs are case sensitive; the estate uses upper case, and a
        # lower-case variant silently fails to match in searchRegistry.
        return value.strip().upper()

    @property
    def display_name(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return name or self.userid

    @property
    def work_item_ids(self) -> list[str]:
        return sorted({s.work_item_id for s in self.source_work_items})


class WorkItem(Strict):
    """A normalised ALM Access Request."""

    work_item_id: str = Field(min_length=1, max_length=32)
    summary: str = ""
    state: str = ""
    access_type: str = ""
    domain: str = ""
    work_areas: str = ""
    justification: str = ""
    new_users_raw: str = Field(
        default="",
        description="The raw New Users field, kept verbatim for audit and re-parsing.")
    created: datetime | None = None
    modified: datetime | None = None
    users: list[RequestedUser] = Field(default_factory=list)


# -------------------------------------------------------------------- statuses

class UserStatus(Strict):
    """The validation agent's verdict on one user."""

    userid: str = Field(pattern=USERID_PATTERN)
    state: UserState = UserState.UNKNOWN
    ldap_name: str = ""
    ldap_email: str = ""
    valid_in_ldap: bool = False
    has_role: bool = False
    role: str = ""
    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)

    @property
    def needs_provisioning(self) -> bool:
        return self.state in (UserState.READY, UserState.ARCHIVED)

    @property
    def blocked(self) -> bool:
        """No amount of retrying will provision this user."""
        return self.state in (UserState.MISSING, UserState.INVALID)


class ProvisionResult(Strict):
    """The result of one write, against one user, for one operation."""

    userid: str = Field(pattern=USERID_PATTERN)
    operation: Operation
    outcome: Outcome
    idempotency_key: str = ""
    work_item_id: str = ""
    message: str = ""
    detail: dict = Field(default_factory=dict)
    replayed: bool = Field(
        default=False,
        description="True when a prior run had already committed this key.")
    at: datetime = Field(default_factory=utcnow)

    @property
    def succeeded(self) -> bool:
        return self.outcome in (Outcome.OK, Outcome.SKIPPED)


# -------------------------------------------------------------------- approval

class ApprovalItem(Strict):
    """One line an approver sees on the card."""

    userid: str
    display_name: str = ""
    work_item_ids: list[str] = Field(default_factory=list)
    action: str = ""
    state: UserState = UserState.UNKNOWN
    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)


class ApprovalRequest(Strict):
    """The batch put in front of a human, fingerprinted so it cannot drift.

    ``plan_hash`` is the same idea as the CLI's approval gate: the plan that was
    shown is the plan that may execute. The graph refuses to write when the
    hash it holds no longer matches what it is about to do.
    """

    thread_id: str
    run_id: str
    environment: str
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    plan_hash: str
    items: list[ApprovalItem] = Field(default_factory=list)

    @property
    def user_count(self) -> int:
        return len(self.items)

    @property
    def work_item_count(self) -> int:
        return len({w for item in self.items for w in item.work_item_ids})


class ApprovalDecision(Strict):
    """What a human decided, and who they were."""

    thread_id: str
    approved: bool
    approver: str = Field(default="", description="Entra object id or UPN of the approver.")
    plan_hash: str = ""
    decided_at: datetime = Field(default_factory=utcnow)
    comment: str = ""
    # Approving a subset: empty means "everything in the request".
    approved_userids: list[str] = Field(default_factory=list)

    def covers(self, userid: str) -> bool:
        if not self.approved:
            return False
        return not self.approved_userids or userid.upper() in {
            u.upper() for u in self.approved_userids}


# ----------------------------------------------------------------------- audit

class AuditEvent(Strict):
    """One immutable row. Written for every decision and every write attempt."""

    run_id: str
    thread_id: str = ""
    at: datetime = Field(default_factory=utcnow)
    environment: str = ""
    step: str
    userid: str = ""
    work_item_id: str = ""
    operation: Operation | None = None
    outcome: Outcome
    idempotency_key: str = ""
    approver: str = ""
    message: str = ""
    detail: dict = Field(default_factory=dict)
    error_type: str = ""

    @classmethod
    def from_result(cls, result: ProvisionResult, *, run_id: str, thread_id: str = "",
                    environment: str = "", step: str = "", approver: str = "") -> AuditEvent:
        return cls(
            run_id=run_id,
            thread_id=thread_id,
            environment=environment,
            step=step or result.operation.value,
            userid=result.userid,
            work_item_id=result.work_item_id,
            operation=result.operation,
            outcome=result.outcome,
            idempotency_key=result.idempotency_key,
            approver=approver,
            message=result.message,
            detail=result.detail,
        )


# ----------------------------------------------------------- idempotency keys

def idempotency_key(work_item_id: str, userid: str, operation: Operation | str) -> str:
    """sha256(work item + user + operation) - stable across runs and processes.

    Deliberately excludes the run id and the timestamp: the whole point is that
    a replayed webhook, a retried node and a manual re-run all derive the *same*
    key and therefore collapse into one write.
    """
    op = operation.value if isinstance(operation, Operation) else str(operation)
    blob = f"{work_item_id}|{userid.upper()}|{op}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def plan_hash(items: list[ApprovalItem]) -> str:
    """Fingerprint of an approval batch: who, on which work items, for what action."""
    rows = sorted(
        f"{i.userid.upper()}:{i.action}:{','.join(sorted(i.work_item_ids))}" for i in items)
    return hashlib.sha256("|".join(rows).encode("utf-8")).hexdigest()
