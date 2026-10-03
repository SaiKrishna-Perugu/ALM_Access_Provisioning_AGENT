"""Who must approve a card before anything is written.

Approval is a decision about people's access, so it follows the usual
separation-of-duties rules, enforced here rather than by habit:

* **How many approvers.** One by default; two in production
  (``ALM_APPROVERS_REQUIRED_PROD``) and for any card with a high-risk user
  (``ALM_APPROVERS_REQUIRED_HIGH_RISK``) - a user missing from LDAP, an ID
  recovered from free text, an account that looks like someone else's.
* **Not your own run.** Whenever two are needed, the person who started the
  run cannot be one of them.
* **One vote each.** A second vote by the same person is refused.
* **Any rejection ends it.** One "no" rejects the card for everyone.
* **What is approved is what everyone approved.** Each approver ticks users;
  only users ticked by every approver are written.

Votes are kept per plan: a card that changes (a new user turned up) needs
fresh votes.
"""
from __future__ import annotations

from dataclasses import dataclass

from alm_core.models import ApprovalDecision


@dataclass
class Tally:
    needed: int
    approvals: list[dict]
    rejection: dict | None

    @property
    def complete(self) -> bool:
        return self.rejection is not None or len(self.approvals) >= self.needed

    def public(self) -> dict:
        return {"needed": self.needed, "approvals": [v["approver"] for v in self.approvals],
                "rejected_by": self.rejection["approver"] if self.rejection else "",
                "complete": self.complete}


class VoteRefused(ValueError):
    """A vote the policy does not accept, with the reason for the approver."""


def approvers_needed(request, settings) -> int:
    needed = int(getattr(settings, "approvers_required", 1) or 1)
    environment = (getattr(request, "environment", "") or
                   getattr(settings, "environment", "")).upper()
    if environment == "PROD":
        needed = max(needed, int(getattr(settings, "approvers_required_prod", 2)))
    if any(str(getattr(item.risk, "value", item.risk)).lower() == "high"
           for item in getattr(request, "items", [])):
        needed = max(needed, int(getattr(settings, "approvers_required_high_risk", 2)))
    return needed


def check_vote(*, approver: str, requested_by: str, needed: int, votes: list[dict]) -> None:
    """Raise VoteRefused if this person may not vote on this card now."""
    who = approver.lower()
    if any(v["approver"] == who for v in votes):
        raise VoteRefused("you have already decided this card; a second approver must "
                          "decide it")
    if needed >= 2 and requested_by and who == requested_by.lower():
        raise VoteRefused("this card needs two approvers, and neither may be the person "
                          "who started the run")


def tally(votes: list[dict], needed: int) -> Tally:
    rejection = next((v for v in votes if not v["approved"]), None)
    return Tally(needed=needed, approvals=[v for v in votes if v["approved"]],
                 rejection=rejection)


def decision(tally_: Tally, *, thread_id: str, plan_hash: str,
             shown: list[str]) -> ApprovalDecision:
    """The single decision the run resumes with, once the tally is complete."""
    if tally_.rejection is not None:
        return ApprovalDecision(
            thread_id=thread_id, approved=False, approver=tally_.rejection["approver"],
            plan_hash=plan_hash,
            comment=tally_.rejection.get("comment") or "rejected in the console")
    chosen = {u.upper() for u in shown}
    for vote in tally_.approvals:
        chosen &= {u.upper() for u in vote["userids"]}
    ordered = [u for u in shown if u.upper() in chosen]
    approvers = " + ".join(v["approver"] for v in tally_.approvals)
    return ApprovalDecision(
        thread_id=thread_id, approved=bool(ordered), approver=approvers,
        plan_hash=plan_hash, approved_userids=ordered,
        comment=(f"approved by {len(tally_.approvals)} of {tally_.needed} required"
                 if ordered else "the approvers ticked no user in common"))
