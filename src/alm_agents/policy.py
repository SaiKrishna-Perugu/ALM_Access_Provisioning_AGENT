"""The rules no agent can talk its way past.

Moving to genuine agency changes where safety lives. Previously the model could
not act, so nothing it decided mattered. Now agents call tools directly and
decide their own next step - so the guarantees have to move into the tool
boundary, and they have to be *mechanical*. A prompt saying "never write without
approval" is a suggestion. This module is not.

Every tool invocation passes through :meth:`PolicyEngine.check` before it runs.
A denial is returned to the agent as an observation, not raised: an agent that
is told "you may not do that, and here is why" can choose a legal action, which
is exactly the adaptivity we wanted. What it cannot do is proceed anyway.

The invariants, in the order they are evaluated:

1. Writes are impossible in shadow mode.
2. Writes require an approval decision naming a human who covers that user.
3. Writes to production require the run to carry a production confirmation.
4. A run has a write budget; an agent that loops cannot drain the estate.
5. User IDs must match the estate's pattern - a hallucinated identifier is
   rejected before it reaches LDAP.
6. Nothing may write to the audit trail except the audit writer itself.
7. Tool calls are budgeted per agent, so a stuck reasoning loop is bounded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from alm_core.logging import get_logger
from alm_core.models import USERID_PATTERN, Operation

log = get_logger("alm.policy")

# Tools that change something outside this process.
WRITE_TOOLS = {
    "provision_jts_user",
    "reactivate_jts_user",
    "request_ad_group_membership",
    "post_workitem_comment",
    "attach_workitem_evidence",
}

# Tools an agent may call freely: they read, or they act only on local state.
READ_TOOLS = {
    "fetch_open_requests",
    "fetch_work_item",
    "parse_new_users_field",
    "recover_user_ids",
    "classify_user",
    "check_jazz_permission",
    "existing_work_item_comments",
    "capture_evidence",
    "recall_memory",
    "remember",
    "handoff",
    "request_human_approval",
    "finish",
}

# No agent may fabricate an audit row; the audit writer is not an agent tool.
FORBIDDEN_TOOLS = {"record_audit_event", "delete_audit", "update_ledger"}


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Verdict(True)


@dataclass
class PolicyEngine:
    """Per-run policy state. One instance per pipeline run."""

    shadow: bool
    environment: str
    approval: object | None = None          # ApprovalDecision
    prod_confirmed: bool = False
    max_writes: int = 50
    max_tool_calls: int = 400
    writes_performed: int = 0
    tool_calls: int = 0
    denials: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------- accounting

    def note_tool_call(self) -> None:
        self.tool_calls += 1

    def note_write(self) -> None:
        self.writes_performed += 1

    def _deny(self, tool: str, reason: str, **context) -> Verdict:
        record = {"tool": tool, "reason": reason, **context}
        self.denials.append(record)
        log.warning("policy_denied", **record)
        return Verdict(False, reason)

    # ------------------------------------------------------------------ check

    def check(self, tool: str, args: dict) -> Verdict:
        """Decide whether one tool call may proceed."""
        if tool in FORBIDDEN_TOOLS:
            return self._deny(tool, "this tool is not available to agents")

        if self.tool_calls >= self.max_tool_calls:
            return self._deny(
                tool, f"the run's tool-call budget ({self.max_tool_calls}) is exhausted; "
                      "hand off or finish")

        userid = str(args.get("userid") or "").strip()
        if userid and not re.match(USERID_PATTERN, userid):
            return self._deny(
                tool, f"{userid!r} is not a valid user ID on this estate; it must match "
                      f"{USERID_PATTERN}. Do not guess or correct user IDs.",
                userid=userid)

        if tool not in WRITE_TOOLS:
            if tool not in READ_TOOLS:
                return self._deny(tool, "unknown tool")
            return ALLOW

        # ---------------------------------------------------------- write path
        if self.shadow:
            return self._deny(
                tool, "shadow mode is on: this run plans but never writes. Record what "
                      "you would do and continue.")

        if self.writes_performed >= self.max_writes:
            return self._deny(
                tool, f"the run's write budget ({self.max_writes}) is exhausted. Stop and "
                      "hand back to the supervisor.")

        if self.approval is None:
            return self._deny(
                tool, "no human approval exists for this run. Call request_human_approval "
                      "and wait for the decision before attempting any write.")

        if not getattr(self.approval, "approved", False):
            return self._deny(tool, "the human rejected this batch; no write is permitted")

        if is_preview_approval(self.approval):
            return self._deny(
                tool, "the only approval is a dry-run preview, which cannot authorise a "
                      "write. A human must approve this batch in a --commit run.")

        if userid and not self.approval.covers(userid):
            return self._deny(
                tool, f"the approval does not cover {userid}. Only these users were "
                      f"approved: {', '.join(self.approval.approved_userids) or 'all listed'}",
                userid=userid)

        if self.environment.upper() == "PROD" and not self.prod_confirmed:
            return self._deny(
                tool, "this run targets PRODUCTION and carries no production "
                      "confirmation; the approval must be re-issued with one")

        return ALLOW

    # ------------------------------------------------------------- reporting

    def summary(self) -> dict:
        return {
            "tool_calls": self.tool_calls,
            "writes": self.writes_performed,
            "prod_confirmed": self.prod_confirmed,
            "denials": len(self.denials),
            "denial_reasons": [d["reason"][:120] for d in self.denials[:10]],
            "shadow": self.shadow,
            "environment": self.environment,
        }


def is_preview_approval(approval) -> bool:
    """True for the automatic approval a dry run records to show its plan."""
    return str(getattr(approval, "approver", "") or "").startswith(PREVIEW_APPROVER_PREFIX)


PREVIEW_APPROVER_PREFIX = "dry-run:"


def operation_for(tool: str) -> Operation | None:
    """Map a write tool onto the operation its idempotency key is derived from."""
    return {
        "provision_jts_user": Operation.JTS_CREATE,
        "reactivate_jts_user": Operation.JTS_UNARCHIVE,
        "request_ad_group_membership": Operation.AD_GROUP_ADD,
        "post_workitem_comment": Operation.WORKITEM_COMMENT,
        "attach_workitem_evidence": Operation.WORKITEM_ATTACH,
    }.get(tool)
