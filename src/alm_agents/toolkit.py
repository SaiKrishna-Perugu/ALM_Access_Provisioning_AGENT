"""The tools agents actually call, and the blackboard they share.

Each tool is a thin, typed wrapper over :mod:`alm_core.tools`. The wrapper adds
three things a model needs and a Python caller does not:

* an **argument schema** the model is held to, so a hallucinated field is a
  schema error rather than a bad call;
* an **observation** written for a reader who must decide what to do next -
  facts and the shape of the problem, never advice like "try again";
* a **blackboard write**, so what one agent discovers is available to the next
  without either of them having to pass it through the model's context.

The blackboard matters more than it looks. Agents that hand structured findings
to each other through prose lose precision at every hop; here the prose is the
conversation and the blackboard is the record.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Protocol

from alm_core.errors import EvidenceInvalid
from alm_core.logging import get_logger
from alm_core.models import (
    WORK_ITEM_ID_PATTERN,
    ApprovalItem,
    Operation,
    ProvisionResult,
    RequestedUser,
    RiskLevel,
    SourceWorkItem,
    UserState,
    UserStatus,
    WorkItem,
)
from alm_core.oslc import parse_new_users
from alm_core.tools import evidence as evidence_tool
from alm_core.tools import ewm, gpt_queue, jts
from alm_core.tools.base import ToolContext, to_thread

from . import llm
from .agent import ToolRegistry, ToolSpec
from .memory import MemoryStore

try:
    from pydantic import BaseModel, Field
except ImportError as err:  # pragma: no cover
    raise ImportError("alm_agents needs the cloud extras: "
                      "pip install -r requirements-cloud.txt") from err

log = get_logger("alm.toolkit")


@dataclass
class Blackboard:
    """Shared, typed findings for one run. Agents read and write it via tools."""

    work_items: dict[str, WorkItem] = field(default_factory=dict)
    users: dict[str, RequestedUser] = field(default_factory=dict)
    statuses: dict[str, UserStatus] = field(default_factory=dict)
    results: list[ProvisionResult] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    verified: set[str] = field(default_factory=set)
    approval_requested: bool = False
    approval_reason: str = ""
    approval_userids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # The work items this run may touch; empty means the whole queue. Set from
    # the run's state by the runtime, not by any agent.
    scope: set[str] = field(default_factory=set)

    def known_names(self) -> list[str]:
        """Every personal name this run holds, for redaction before a model."""
        names: list[str] = []
        for user in self.users.values():
            names += [user.first_name, user.last_name, user.display_name]
        for status in self.statuses.values():
            names.append(status.ldap_name)
        return [n for n in names if n]

    def out_of_scope(self, work_item_ids) -> list[str]:
        """The given work items that this run may not touch."""
        if not self.scope:
            return []
        return sorted(w for w in work_item_ids if w not in self.scope)

    def approval_items(self) -> list[ApprovalItem]:
        """The card the human sees, assembled from what the agents established."""
        action_for = {
            UserState.READY: "import into JTS",
            UserState.ARCHIVED: "reactivate archived account",
            UserState.EXISTS: "no change (already active)",
            UserState.MISSING: "cannot provision - not in LDAP",
            UserState.INVALID: "cannot provision - LDAP entry invalid",
            UserState.UNKNOWN: "unknown - not validated",
        }
        items: list[ApprovalItem] = []
        # Every user in the run goes on the card, not only the ones an agent
        # selected: the approval then covers exactly what the human saw, and
        # anyone added later has to come back for a new decision.
        for userid in sorted(set(self.approval_userids) | set(self.users)):
            user = self.users.get(userid)
            if user is None:
                continue
            status = self.statuses.get(userid)
            if status is None:
                # An agent asked for approval before validating this user. Say so
                # on the card rather than presenting an unknown as routine.
                items.append(ApprovalItem(
                    userid=userid, display_name=user.display_name,
                    work_item_ids=user.work_item_ids,
                    action=action_for[UserState.UNKNOWN],
                    state=UserState.UNKNOWN, risk=RiskLevel.HIGH,
                    risk_reasons=["not validated before approval was requested"]))
                continue
            items.append(ApprovalItem(
                userid=userid, display_name=user.display_name,
                work_item_ids=user.work_item_ids,
                action=action_for.get(status.state, status.state.value),
                state=status.state, risk=status.risk,
                risk_reasons=status.risk_reasons))
        items.sort(key=lambda i: (i.risk != RiskLevel.HIGH, i.userid))
        return items

    # -------------------------------------------------- checkpoint support

    def to_state(self) -> dict:
        """A JSON-safe snapshot for the graph checkpoint.

        The blackboard is a live object held by the runtime, but a run parked at
        the approval gate may resume in a different process hours later. Without
        this, everything the planning agents established would be lost and the
        provisioner would resume with an empty view of the world.
        """
        return {
            "work_items": {k: v.model_dump(mode="json")
                           for k, v in self.work_items.items()},
            "users": {k: v.model_dump(mode="json") for k, v in self.users.items()},
            "statuses": {k: v.model_dump(mode="json")
                         for k, v in self.statuses.items()},
            "results": [r.model_dump(mode="json") for r in self.results],
            "evidence": dict(self.evidence),
            "verified": sorted(self.verified),
            "approval_requested": self.approval_requested,
            "approval_reason": self.approval_reason,
            "approval_userids": list(self.approval_userids),
            "notes": list(self.notes),
        }

    def load_state(self, data: dict | None) -> Blackboard:
        """Rehydrate in place from a checkpoint snapshot."""
        if not data:
            return self
        self.work_items = {k: WorkItem.model_validate(v)
                           for k, v in (data.get("work_items") or {}).items()}
        self.users = {k: RequestedUser.model_validate(v)
                      for k, v in (data.get("users") or {}).items()}
        self.statuses = {k: UserStatus.model_validate(v)
                         for k, v in (data.get("statuses") or {}).items()}
        self.results = [ProvisionResult.model_validate(r)
                        for r in (data.get("results") or [])]
        self.evidence = dict(data.get("evidence") or {})
        self.verified = set(data.get("verified") or [])
        self.approval_requested = bool(data.get("approval_requested"))
        self.approval_reason = data.get("approval_reason", "")
        self.approval_userids = list(data.get("approval_userids") or [])
        self.notes = list(data.get("notes") or [])
        return self

    def snapshot(self) -> str:
        """A compact view for an agent's opening context."""
        return json.dumps({
            "work_items": sorted(self.work_items),
            "users": {u: (self.statuses[u].state.value if u in self.statuses
                          else "not validated") for u in sorted(self.users)},
            "verified": sorted(self.verified),
            "writes_recorded": len(self.results),
            "evidence_captured": sorted(self.evidence),
            "notes": self.notes[-5:],
            "scope": sorted(self.scope) or "the whole active queue",
        }, indent=2)


# --------------------------------------------------------------- arg schemas

class NoArgs(BaseModel):
    pass


class FetchQueueArgs(BaseModel):
    limit: int = Field(default=25, ge=1, le=200,
                       description="Maximum work items to return.")


class WorkItemArgs(BaseModel):
    work_item_id: str = Field(pattern=WORK_ITEM_ID_PATTERN,
                              description="The numeric EWM work item identifier.")


class ParseArgs(BaseModel):
    work_item_id: str = Field(
        default="", pattern=r"^([0-9]{1,10})?$",
        description="A fetched work item: its stored New Users field is parsed.")
    text: str = Field(default="", description="Other text to parse, when there is no work item.")


class RecoverArgs(BaseModel):
    work_item_id: str = Field(pattern=WORK_ITEM_ID_PATTERN,
                              description="A work item already fetched in this run.")


class UserArgs(BaseModel):
    userid: str = Field(description="The Jazz user ID, e.g. AB12345.")


class CommentArgs(BaseModel):
    work_item_id: str = Field(pattern=WORK_ITEM_ID_PATTERN,
                              description="Work item to comment on. The comment text is "
                                          "written by the tool from what this run "
                                          "recorded; you do not supply it.")


class AttachArgs(BaseModel):
    work_item_id: str = Field(pattern=WORK_ITEM_ID_PATTERN)
    userid: str


class CaptureArgs(BaseModel):
    userids: list[str] = Field(description="Users whose JTS profile to screenshot.")


class RecallArgs(BaseModel):
    subject: str = Field(default="", description="A user ID or work item id.")
    tags: list[str] = Field(default_factory=list)
    kind: str = Field(default="", description="'episodic', 'semantic', or empty for both.")


class RememberArgs(BaseModel):
    kind: str = Field(description="'episodic' for a scoped fact, 'semantic' for a hint.")
    content: str = Field(description="One sentence. Factual, and useful to a later run.")
    subject: str = Field(default="", description="User ID or work item this concerns.")
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ApprovalArgs(BaseModel):
    reason: str = Field(description="Why this batch needs a human, in one sentence.")
    userids: list[str] = Field(default_factory=list,
                               description="Users to include. Empty means all validated.")


class HandoffArgs(BaseModel):
    to: str = Field(description="The agent to hand to.")
    task: str = Field(description="A specific instruction. Not 'continue'.")


class FinishArgs(BaseModel):
    summary: str = Field(description="What you established or did. Factual.")
    complete: bool = Field(default=True,
                           description="False if you stopped without finishing.")
    blocked_on: str = Field(default="", description="What stopped you, if anything.")


# ------------------------------------------------------------------- backend

class Backend(Protocol):
    """The systems the tools reach: EWM, the JTS registry, the AD queue, a browser.

    The agents, their prompts, the policy engine and the write guard do not
    change with the backend - only where the reads land and what a write
    touches. ``LiveBackend`` is production. ``alm_agents.sandbox`` supplies a
    simulated estate so the whole agentic system can run on a laptop.
    """

    async def fetch_open_requests(self, ctx: ToolContext, limit: int
                                  ) -> list[WorkItem]: ...
    async def fetch_work_item(self, ctx: ToolContext, work_item_id: str
                              ) -> WorkItem | None: ...
    async def existing_comments(self, ctx: ToolContext, work_item_id: str
                                ) -> list[str] | None: ...
    async def classify_user(self, ctx: ToolContext, user: RequestedUser
                            ) -> UserStatus: ...
    async def check_role(self, ctx: ToolContext, userid: str) -> bool: ...
    async def capture_profiles(self, ctx: ToolContext, userids: list[str],
                               out_dir: str) -> dict[str, str]: ...
    async def provision_user(self, ctx: ToolContext, user: RequestedUser,
                             status: UserStatus) -> ProvisionResult: ...
    async def request_group_membership(self, ctx: ToolContext, user: RequestedUser,
                                       *, group: str, domain: str
                                       ) -> ProvisionResult: ...
    async def post_comment(self, ctx: ToolContext, *, work_item_id: str, userid: str,
                           text: str, marker: str) -> ProvisionResult: ...
    async def attach_evidence(self, ctx: ToolContext, *, work_item_id: str,
                              userid: str, path: str, filename: str
                              ) -> ProvisionResult: ...


class LiveBackend:
    """The real corporate systems, through :mod:`alm_core.tools`."""

    async def fetch_open_requests(self, ctx, limit):
        return await ewm.fetch_open_requests(ctx, limit)

    async def fetch_work_item(self, ctx, work_item_id):
        return await ewm.fetch_work_item(ctx, work_item_id)

    async def existing_comments(self, ctx, work_item_id):
        # _existing_comments is a blocking requests call; off the event loop it goes.
        return await to_thread(ewm._existing_comments, ctx, work_item_id)

    async def classify_user(self, ctx, user):
        return await jts.classify_user(ctx, user)

    async def check_role(self, ctx, userid):
        return await jts.check_role(ctx, userid)

    async def capture_profiles(self, ctx, userids, out_dir):
        return await evidence_tool.capture_profiles(ctx, userids, out_dir)

    async def provision_user(self, ctx, user, status):
        return await jts.provision_user(ctx, user, status)

    async def request_group_membership(self, ctx, user, *, group, domain):
        return await gpt_queue.request_group_membership(ctx, user, group=group,
                                                        domain=domain)

    async def post_comment(self, ctx, *, work_item_id, userid, text, marker):
        return await ewm.post_comment(ctx, work_item_id=work_item_id, userid=userid,
                                      text=text, marker=marker)

    async def attach_evidence(self, ctx, *, work_item_id, userid, path, filename):
        return await ewm.attach_evidence(ctx, work_item_id=work_item_id, userid=userid,
                                         path=path, filename=filename)


# ------------------------------------------------------------------- builder

def build_registry(ctx: ToolContext, board: Blackboard, memory: MemoryStore,
                   *, shots_dir: str = "", backend: Backend | None = None) -> ToolRegistry:
    """Assemble the tools for one run, closed over its context and blackboard."""
    backend = backend or LiveBackend()
    shots_dir = shots_dir or os.path.join(tempfile.gettempdir(), "alm-evidence",
                                          ctx.run_id or "adhoc")
    # Users whose board status was read from LDAP/JTS in this process and has
    # not been touched since. Several agents classify the same user; only a
    # write or a changed permission can make that answer stale.
    fresh: set[str] = set()

    # ------------------------------------------------------------ read tools

    async def fetch_open_requests(limit: int = 25) -> str:
        if board.scope:
            # A scoped run reads exactly its work items, whether or not they are
            # still in an active state - never the rest of the queue.
            items = [i for i in [await backend.fetch_work_item(ctx, w)
                                 for w in sorted(board.scope)] if i is not None]
        else:
            items = await backend.fetch_open_requests(ctx, limit)
        for item in items:
            board.work_items[item.work_item_id] = item
            for user in item.users:
                board.users.setdefault(user.userid, user)
        if not items:
            if board.scope:
                return (f"none of this run's work items ({', '.join(sorted(board.scope))}) "
                        "exist in the configured project area - check the IDs; there is "
                        "nothing to do")
            return "no open requests in the queue - there is nothing to do"
        return json.dumps([{
            "work_item_id": i.work_item_id, "summary": i.summary, "state": i.state,
            "parsed_users": [u.userid for u in i.users],
            "new_users_field_present": bool(i.new_users_raw),
        } for i in items], indent=2)

    async def fetch_work_item(work_item_id: str) -> str:
        if board.out_of_scope([work_item_id]):
            return (f"DENIED: work item {work_item_id} is outside this run's scope "
                    f"({', '.join(sorted(board.scope))}). Work only on those.")
        item = await backend.fetch_work_item(ctx, work_item_id)
        if item is None:
            return f"no work item {work_item_id} in the configured project area"
        board.work_items[item.work_item_id] = item
        for user in item.users:
            board.users.setdefault(user.userid, user)
        return json.dumps({
            "work_item_id": item.work_item_id, "summary": item.summary,
            "state": item.state, "justification": item.justification[:500],
            "new_users_raw": item.new_users_raw[:1000],
            "parsed_users": [u.userid for u in item.users],
        }, indent=2)

    async def parse_new_users_field(work_item_id: str = "", text: str = "") -> str:
        item = board.work_items.get(work_item_id) if work_item_id else None
        if item is not None:
            # Parse the field as stored, never the model's copy of it: the model
            # sees e-mail addresses redacted, and a redacted row is rejected as
            # malformed - a defect invented by the redaction, not in the data.
            text, source = item.new_users_raw, f"the stored New Users field of {work_item_id}"
        elif work_item_id:
            return f"ERROR: work item {work_item_id} is not in this run. Fetch it first."
        else:
            source = "the text you supplied"
        records, rejected = parse_new_users(text)
        return json.dumps({
            "source": source,
            "parsed": records,
            "rejected_rows": rejected,
            "note": ("Rejected rows did not match LASTNAME,FIRSTNAME,email,USERID. "
                     "Recover a user ID from them ONLY if it is present verbatim."),
        }, indent=2)

    async def recover_user_ids(work_item_id: str) -> str:
        item = board.work_items.get(work_item_id)
        if item is None:
            return f"ERROR: work item {work_item_id} is not in this run. Fetch it first."
        _records, rejected = parse_new_users(item.new_users_raw)
        text = "; ".join(rejected)
        where = "rejected New Users rows"
        if not item.new_users_raw.strip():
            text, where = item.justification, "Justification field"
        if not text.strip():
            return f"nothing to recover: {work_item_id} has no rejected rows"

        recovery = await asyncio.to_thread(llm.recover_userids, ctx.settings, text,
                                           work_item_id, item.summary)
        source = SourceWorkItem(work_item_id=work_item_id, summary=item.summary)
        parsed = {u.userid for u in item.users}
        added = []
        for userid, probability in recovery.accepted.items():
            if userid in parsed or userid in board.users:
                continue
            board.users[userid] = RequestedUser(
                userid=userid, source_work_items=[source], extracted_by_llm=True,
                extraction_confidence=probability)
            added.append(userid)
        return json.dumps({
            "searched": where,
            "judged_by": recovery.method,
            "candidates": recovery.candidates,
            "requested": recovery.accepted,
            "not_requested": recovery.rejected,
            "added_to_run": added,
            "note": ("Only these candidates exist - they are every user-ID-shaped token "
                     "in the text. Do not add any other ID. Added users are flagged as "
                     "machine-recovered and go to the approver with their probability. "
                     + ("No judge was available: leave these rows for a human."
                        if recovery.candidates and recovery.method == "none" else "")),
        }, indent=2)

    async def classify_user(userid: str) -> str:
        if userid in fresh and userid in board.statuses:
            return _status_observation(board.statuses[userid], cached=True)
        user = board.users.get(userid)
        if user is None:
            # No fetched work item's structured field produced this ID, so it
            # came from a model (the extractor recovering a malformed row, most
            # likely). Say so: _risk() then makes it HIGH and the approver sees
            # that a machine proposed it.
            user = RequestedUser(userid=userid, extracted_by_llm=True)
        board.users.setdefault(userid, user)
        status = await backend.classify_user(ctx, user)
        board.statuses[userid] = status
        fresh.add(userid)
        return _status_observation(status, cached=False)

    def _status_observation(status: UserStatus, *, cached: bool) -> str:
        observation = {
            "userid": status.userid, "registry_state": status.state.value,
            "ldap_name": status.ldap_name, "ldap_email": status.ldap_email,
            "valid_in_ldap": status.valid_in_ldap,
            "already_has_role": status.has_role,
            "risk": status.risk.value, "risk_reasons": status.risk_reasons,
        }
        if cached:
            observation["note"] = ("read earlier in this run; nothing has been written "
                                   "for this user since, so this is still current")
        return json.dumps(observation, indent=2)

    async def check_jazz_permission(userid: str) -> str:
        has = await backend.check_role(ctx, userid)
        status = board.statuses.get(userid)
        if status is not None and status.has_role != has:
            fresh.discard(userid)
        if has:
            board.verified.add(userid)
        return (f"{userid} {'HAS' if has else 'does NOT have'} the "
                f"{ctx.settings.jazz_role} repository permission"
                + ("" if has else ". Propagation can take up to 30 minutes after "
                                  "provisioning; this is not necessarily a failure."))

    async def existing_work_item_comments(work_item_id: str) -> str:
        comments = await backend.existing_comments(ctx, work_item_id)
        if comments is None:
            return ("could not read existing comments - a duplicate check is not "
                    "possible for this work item right now")
        return json.dumps({"count": len(comments),
                           "comments": [c[:300] for c in comments[-10:]]}, indent=2)

    async def capture_evidence(userids: list[str]) -> str:
        try:
            artifacts = await backend.capture_profiles(ctx, userids, shots_dir)
        except EvidenceInvalid as err:
            problems = err.context.get("problems", [])
            return ("DENIED-EQUIVALENT: evidence validation failed, nothing was captured "
                    "for upload. Do not retry without a different approach. Problems: "
                    + "; ".join(problems))
        board.evidence.update(artifacts)
        missing = [u for u in userids if u not in artifacts]
        attach_to = {u: board.users[u].work_item_ids for u in sorted(artifacts)
                     if u in board.users}
        return json.dumps({"captured": sorted(artifacts),
                           "not_confirmed": missing,
                           "saved_in": os.path.abspath(shots_dir),
                           "attach_to": attach_to,
                           "note": ("Only captured users may be attached, and only to the "
                                    "work items listed for them in attach_to - never to "
                                    "another work item.")}, indent=2)

    # ----------------------------------------------------------- write tools

    def _scope_denial(userid: str = "", work_item_id: str = "") -> str:
        """A DENIED observation when a write would reach outside the run's scope."""
        if not board.scope:
            return ""
        if work_item_id:
            touched = [work_item_id]
        else:
            user = board.users.get(userid)
            touched = user.work_item_ids if user is not None else []
            if not touched:
                return (f"DENIED: {userid} is not attributed to any work item in this "
                        "run's scope, so it may not be written. Leave it for a human.")
        outside = board.out_of_scope(touched)
        if outside:
            return (f"DENIED: work item {', '.join(outside)} is outside this run's scope "
                    f"({', '.join(sorted(board.scope))}).")
        return ""

    def _record(result: ProvisionResult) -> str:
        board.results.append(result)
        # Whatever the outcome, the user's registry state may have moved.
        fresh.discard(result.userid)
        return json.dumps({"userid": result.userid,
                           "operation": result.operation.value,
                           "outcome": result.outcome.value,
                           "replayed": result.replayed,
                           "message": result.message}, indent=2)

    async def provision_jts_user(userid: str) -> str:
        if denial := _scope_denial(userid=userid):
            return denial
        user = board.users.get(userid)
        status = board.statuses.get(userid)
        if user is None:
            return f"ERROR: {userid} is not in this run. Fetch its work item first."
        if status is None:
            return (f"ERROR: {userid} has not been validated. Call classify_user first - "
                    "provisioning without knowing the registry state is not permitted.")
        if status.state == UserState.ARCHIVED:
            return ("ERROR: this account is archived. Use reactivate_jts_user, which "
                    "clears the archived flag instead of creating a duplicate.")
        return _record(await backend.provision_user(ctx, user, status))

    async def reactivate_jts_user(userid: str) -> str:
        if denial := _scope_denial(userid=userid):
            return denial
        user = board.users.get(userid)
        status = board.statuses.get(userid)
        if user is None or status is None:
            return f"ERROR: validate {userid} with classify_user first."
        if status.state != UserState.ARCHIVED:
            return (f"ERROR: {userid} is {status.state.value}, not archived. "
                    "Reactivation does not apply.")
        return _record(await backend.provision_user(ctx, user, status))

    async def request_ad_group_membership(userid: str) -> str:
        if denial := _scope_denial(userid=userid):
            return denial
        user = board.users.get(userid)
        if user is None:
            return f"ERROR: {userid} is not in this run."
        result = await backend.request_group_membership(
            # GROUP_NAME / DOMAIN are the CLI's names for the same settings, so an
            # existing .env works unchanged.
            ctx, user,
            group=os.getenv("ALM_AD_GROUP") or os.getenv("GROUP_NAME") or "GR_D-JazzUser-NA",
            domain=os.getenv("ALM_AD_DOMAIN") or os.getenv("DOMAIN") or "INETPSA")
        observation = _record(result)
        if result.outcome.value != "ok":
            return observation
        # Deliberately worded: the agent must not report a queued request as
        # completed group membership.
        return json.dumps({"userid": userid, "outcome": "submitted",
                           "note": ("The AD change is QUEUED, not applied. Do not report "
                                    "it as complete. check_jazz_permission is what "
                                    "confirms it landed.")}, indent=2)

    async def post_workitem_comment(work_item_id: str) -> str:
        from .nodes.closure import action_from_records, render_comment

        if denial := _scope_denial(work_item_id=work_item_id):
            return denial
        # The comment is the permanent record on someone else's work item, so
        # its words come from the records, never from the model: an agent (or
        # text a requester planted in the work item) can decide *whether* to
        # comment, but cannot make the comment claim anything unrecorded.
        def name_for(userid: str, user: RequestedUser) -> str:
            # A user recovered from free text has no requester-given name, so
            # display_name falls back to the ID ("TB22322: TB22322"). LDAP knows
            # the name; use it before repeating the ID.
            if user.first_name or user.last_name:
                return user.display_name
            status = board.statuses.get(userid)
            return (status.ldap_name if status and status.ldap_name else userid)

        entries = [
            (userid, name_for(userid, user),
             action_from_records(userid, board.results, board.statuses))
            for userid, user in sorted(board.users.items())
            if userid in board.verified and work_item_id in user.work_item_ids]
        if not entries:
            return (f"ERROR: no verified user on work item {work_item_id}, so there is "
                    "nothing true to report yet. Verify first (check_jazz_permission).")
        # Say only what nobody has said yet - an earlier run of the agents, or
        # the CLI, may already have reported some of these users.
        from idempotency import already_reported

        from .nodes.closure import ACTION_TEXT

        existing = await backend.existing_comments(ctx, work_item_id)
        if existing is not None:
            entries = [e for e in entries if not already_reported(
                existing, e[0], ACTION_TEXT.get(e[2], ACTION_TEXT["unknown"]))]
            if not entries:
                return json.dumps({
                    "work_item_id": work_item_id, "outcome": "skipped",
                    "note": ("every verified user's outcome is already reported on "
                             "this work item (by an earlier run or the CLI); nothing "
                             "new to say, so nothing was posted")}, indent=2)
        text = render_comment(entries)
        result = await backend.post_comment(ctx, work_item_id=work_item_id,
                                            userid=entries[0][0], text=text, marker="")
        observation = json.loads(_record(result))
        observation["posted_text"] = text
        return json.dumps(observation, indent=2)

    async def attach_workitem_evidence(work_item_id: str, userid: str) -> str:
        if denial := _scope_denial(work_item_id=work_item_id):
            return denial
        # A profile screenshot is personal data: it belongs only on the work
        # items that asked for that person, never on another team's request.
        user = board.users.get(userid)
        if user is None or work_item_id not in user.work_item_ids:
            requested_on = ", ".join(user.work_item_ids) if user else "no work item in this run"
            return (f"DENIED: {userid} was not requested on work item {work_item_id} "
                    f"(requested on: {requested_on}). Attach evidence only to the work "
                    "items that requested that user.")
        path = board.evidence.get(userid)
        if not path:
            return (f"ERROR: no validated evidence for {userid}. Call capture_evidence "
                    "first; if it refused, nothing may be attached for anyone.")
        result = await backend.attach_evidence(ctx, work_item_id=work_item_id,
                                               userid=userid, path=path,
                                               filename=f"{userid}.png")
        return _record(result)

    # --------------------------------------------------------- memory tools

    async def recall_memory(subject: str = "", tags: list[str] | None = None,
                            kind: str = "") -> str:
        rows = await memory.recall(subject=subject, tags=tags or [], kind=kind)
        if not rows:
            return "no memories match"
        return json.dumps([{k: str(v) for k, v in row.items()} for row in rows],
                          indent=2)

    async def remember(kind: str, content: str, subject: str = "",
                       tags: list[str] | None = None, confidence: float = 0.5) -> str:
        return await memory.remember(kind=kind, content=content, subject=subject,
                                     tags=tags or [], author="agent",
                                     run_id=ctx.run_id, confidence=confidence)

    # -------------------------------------------------------- control tools

    async def request_human_approval(reason: str, userids: list[str] | None = None) -> str:
        board.approval_requested = True
        board.approval_reason = reason
        board.approval_userids = [u for u in (userids or []) if u in board.users]
        count = len(board.approval_userids or board.users)
        return (f"Approval requested for {count} user(s). The run will pause at the "
                "approval gate once the planning agents finish. You cannot write until "
                "a human decides.")

    async def handoff(to: str, task: str) -> str:
        return f"handing off to {to}: {task}"

    async def finish(summary: str, complete: bool = True, blocked_on: str = "") -> str:
        return summary if complete else f"{summary} (incomplete: {blocked_on})"

    # ------------------------------------------------------------- assembly

    registry = ToolRegistry([
        ToolSpec("fetch_open_requests",
                 "List ALM Access Request work items currently in the active queue.",
                 FetchQueueArgs, fetch_open_requests),
        ToolSpec("fetch_work_item",
                 "Read one work item in full, including the raw New Users field.",
                 WorkItemArgs, fetch_work_item),
        ToolSpec("parse_new_users_field",
                 "Run the deterministic parser over New Users text; returns parsed "
                 "records and the rows it rejected.",
                 ParseArgs, parse_new_users_field),
        ToolSpec("recover_user_ids",
                 "Recover user IDs from rows the parser rejected. Code lists every "
                 "user-ID-shaped token in the text and a model judges which are people "
                 "being requested; it can never return an ID that is not in the text.",
                 RecoverArgs, recover_user_ids),
        ToolSpec("classify_user",
                 "Look a user up in LDAP and the JTS registry; returns state, validity, "
                 "whether they already hold the role, and a risk assessment.",
                 UserArgs, classify_user),
        ToolSpec("check_jazz_permission",
                 "Check whether a user currently holds the JazzUsers repository role. "
                 "This is the only confirmation that access actually landed.",
                 UserArgs, check_jazz_permission),
        ToolSpec("existing_work_item_comments",
                 "Read the comments already on a work item, to avoid duplicating one.",
                 WorkItemArgs, existing_work_item_comments),
        ToolSpec("capture_evidence",
                 "Screenshot JTS profiles and validate the batch. Refuses entirely if "
                 "two users produce identical artifacts.",
                 CaptureArgs, capture_evidence),

        ToolSpec("provision_jts_user",
                 "Import a validated, LDAP-present user into the JTS registry. WRITE.",
                 UserArgs, provision_jts_user, is_write=True),
        ToolSpec("reactivate_jts_user",
                 "Clear the archived flag on an existing JTS contributor. WRITE.",
                 UserArgs, reactivate_jts_user, is_write=True),
        ToolSpec("request_ad_group_membership",
                 "Queue an AD group addition for the Windows worker. Queued, not "
                 "applied. WRITE.",
                 UserArgs, request_ad_group_membership, is_write=True),
        ToolSpec("post_workitem_comment",
                 "Post the status comment on a work item, written from this run's "
                 "records for its verified users. Idempotent. WRITE.",
                 CommentArgs, post_workitem_comment, is_write=True),
        ToolSpec("attach_workitem_evidence",
                 "Attach a captured, validated profile screenshot to a work item. WRITE.",
                 AttachArgs, attach_workitem_evidence, is_write=True),

        ToolSpec("recall_memory",
                 "Retrieve what previous runs recorded about a user, work item or topic.",
                 RecallArgs, recall_memory),
        ToolSpec("remember",
                 "Record a fact or a hint for future runs. Be specific and factual.",
                 RememberArgs, remember),

        ToolSpec("request_human_approval",
                 "Ask a human to approve this batch. Required before any write.",
                 ApprovalArgs, request_human_approval),
        ToolSpec("handoff",
                 "Hand the work to another agent with a specific task.",
                 HandoffArgs, handoff, terminal=True),
        ToolSpec("finish",
                 "Declare your part done and summarise what you established.",
                 FinishArgs, finish, terminal=True),
    ])
    return registry


def context_for(board: Blackboard, memory_brief: str = "") -> str:
    """The opening context handed to every agent."""
    parts = [f"Current run state:\n{board.snapshot()}"]
    if memory_brief:
        parts.append(memory_brief)
    return "\n\n".join(parts)


def operation_of(tool: str) -> Operation | None:
    from .policy import operation_for

    return operation_for(tool)


def as_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2)
