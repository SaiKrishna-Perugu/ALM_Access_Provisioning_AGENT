"""The approval gate must bind the reviewed plan to the executed plan.

Observed on 2026-08-27: a dry run at 14:52 showed 4 work items / 7 users, and by
00:45 the queue held 13 work items / 17 users. The commit run re-queried the live
queue, so users the operator never saw were swept into a production write.
"""
from __future__ import annotations

import plan_lock


def user(uid: str, *work_items: str) -> dict:
    return {"userid": uid, "email": f"{uid}@example.com",
            "source_work_items": [{"work_item_id": w} for w in work_items]}


def test_identical_plans_match():
    plan = [user("AB12345", "4348411"), user("CD67890", "4348690")]
    a = plan_lock.plan_summary(plan)
    b = plan_lock.plan_summary(list(reversed(plan)))
    assert a["hash"] == b["hash"], "ordering must not change the fingerprint"


def test_a_new_user_breaks_the_match():
    approved = plan_lock.plan_summary([user("AB12345", "4348411")])
    retrieved = plan_lock.plan_summary([user("AB12345", "4348411"),
                                        user("CD67890", "4348690")])
    ok, message = plan_lock.check(approved, retrieved)
    assert not ok
    assert "CD67890" in message
    assert "4348690" in message


def test_a_removed_user_breaks_the_match():
    approved = plan_lock.plan_summary([user("AB12345", "4348411"), user("CD67890", "4348690")])
    retrieved = plan_lock.plan_summary([user("AB12345", "4348411")])
    ok, message = plan_lock.check(approved, retrieved)
    assert not ok
    assert "vanished since approval" in message


def test_the_same_user_gaining_a_work_item_breaks_the_match():
    approved = plan_lock.plan_summary([user("AB12345", "4348411")])
    retrieved = plan_lock.plan_summary([user("AB12345", "4348411", "4348690")])
    ok, message = plan_lock.check(approved, retrieved)
    assert not ok
    assert "4348690" in message


def test_cosmetic_changes_do_not_force_reapproval():
    """A display name corrected in LDAP is not a change to what was approved."""
    before = [{"userid": "AB12345", "first_name": "ADA", "last_name": "LOVELACE",
               "email": "ada@example.com",
               "source_work_items": [{"work_item_id": "4348411", "summary": "old"}]}]
    after = [{"userid": "AB12345", "first_name": "Ada", "last_name": "Lovelace",
              "email": "ada.lovelace@example.com",
              "source_work_items": [{"work_item_id": "4348411", "summary": "edited summary"}]}]
    ok, _ = plan_lock.check(plan_lock.plan_summary(before), plan_lock.plan_summary(after))
    assert ok


def test_commit_without_any_approved_plan_is_refused():
    ok, message = plan_lock.check(None, plan_lock.plan_summary([user("AB12345", "1")]))
    assert not ok
    assert "No approved plan" in message


def test_empty_stored_plan_is_refused():
    ok, _ = plan_lock.check({}, plan_lock.plan_summary([user("AB12345", "1")]))
    assert not ok


def test_summary_counts_users_and_work_items():
    summary = plan_lock.plan_summary([user("AB12345", "1", "2"), user("CD67890", "2")])
    assert summary["user_count"] == 2
    assert summary["work_item_count"] == 2
    assert summary["work_items"] == ["1", "2"]


def test_users_without_an_id_are_ignored():
    summary = plan_lock.plan_summary([user("AB12345", "1"), {"userid": "  "}])
    assert summary["users"] == ["AB12345"]
