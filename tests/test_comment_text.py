"""The comment must describe what the run actually did.

The production defect: every comment said "User added to JTS" for every user,
including the ten the import had reported as "already a JTS user (active)" and
never touched. That is a false statement written permanently onto someone else's
work item.
"""
from __future__ import annotations

import audit
import ewm_comment_workitems as cw
import idempotency

MEMBERS = [{"userid": "AB12345", "name": "Ada Lovelace"},
           {"userid": "CD67890", "name": "Grace Hopper"}]


def test_created_user_is_described_as_added():
    text = cw.build_comment([MEMBERS[0]], {"AB12345": {"action": "created", "state": "active"}})
    assert "AB12345: Ada Lovelace: User added to JTS - (active)" in text


def test_preexisting_user_is_not_claimed_as_added():
    text = cw.build_comment([MEMBERS[0]],
                            {"AB12345": {"action": "already_active", "state": "active"}})
    assert "already present in JTS" in text
    assert "User added to JTS" not in text


def test_unarchived_user_says_reactivated():
    text = cw.build_comment([MEMBERS[0]],
                            {"AB12345": {"action": "unarchived", "state": "active"}})
    assert "reactivated" in text.lower()
    assert "User added to JTS" not in text


def test_mixed_batch_describes_each_user_separately():
    text = cw.build_comment(MEMBERS, {
        "AB12345": {"action": "created", "state": "active"},
        "CD67890": {"action": "already_active", "state": "active"},
    })
    assert "AB12345: Ada Lovelace: User added to JTS" in text
    assert "CD67890: Grace Hopper: User already present in JTS" in text


def test_unknown_outcome_makes_no_claim_about_adding():
    text = cw.build_comment([MEMBERS[0]], {})
    assert "User added to JTS" not in text
    assert "confirmed in JTS" in text


def test_plain_string_status_map_is_still_accepted():
    """Backwards compatible with the old {uid: state} shape."""
    text = cw.build_comment([MEMBERS[0]], {"AB12345": "archived"})
    assert "(archived)" in text


def test_comment_carries_a_marker_when_a_work_item_is_given():
    text = cw.build_comment(MEMBERS, {"AB12345": {"action": "created", "state": "active"},
                                      "CD67890": {"action": "created", "state": "active"}},
                            "4348411")
    marker = idempotency.comment_marker(
        "4348411", [{"userid": "AB12345", "status": "created/active"},
                    {"userid": "CD67890", "status": "created/active"}])
    assert text.strip().endswith(marker)


def test_no_marker_without_a_work_item():
    text = cw.build_comment(MEMBERS, {})
    assert idempotency.MARKER_RE.search(text) is None


def test_status_from_audit_reads_the_import_outcomes(workdir):
    audit.record("import", "AB12345", audit.OK, outcome="created")
    audit.record("import", "CD67890", audit.OK, outcome="already_active")
    audit.flush("import")

    status = cw.status_from_audit(["AB12345", "CD67890", "EF11111"])
    assert status["AB12345"]["action"] == "created"
    assert status["CD67890"]["action"] == "already_active"
    # A user the import never recorded gets no invented action.
    assert status["EF11111"]["action"] == "unknown"


def test_merge_status_prefers_the_live_jts_state(workdir):
    merged = cw.merge_status({"AB12345": {"action": "created", "state": "active"}},
                             {"AB12345": "archived"})
    assert merged["AB12345"] == {"action": "created", "state": "archived"}


def test_group_by_workitem_lists_a_shared_user_under_each_item(users):
    order, wi_users, summaries = cw.group_by_workitem(users)
    assert order == ["4348411", "4348690"]
    assert [m["userid"] for m in wi_users["4348411"]] == ["AB12345", "CD67890"]
    assert [m["userid"] for m in wi_users["4348690"]] == ["CD67890"]
    assert summaries["4348411"] == "Grant ALM access"


def test_group_by_workitem_uses_the_user_id_when_no_name_is_known():
    order, wi_users, _ = cw.group_by_workitem(
        [{"userid": "AB12345", "source_work_items": [{"work_item_id": "1"}]}])
    assert order == ["1"]
    assert wi_users["1"][0]["name"] == "AB12345"
