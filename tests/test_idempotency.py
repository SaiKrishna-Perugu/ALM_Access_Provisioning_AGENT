"""Re-running a commit must not duplicate what it already wrote.

The 2026-09-01 run posted 13 of 17 comments and then died on a connection reset.
The only way forward was a re-run, which would have posted those 13 a second
time - and re-uploaded every screenshot, because the "already linked" check
compared attachment URLs and a re-upload always produces a new one.
"""
from __future__ import annotations

import idempotency

MEMBERS = [{"userid": "AB12345", "status": "created/active"},
           {"userid": "CD67890", "status": "already_active/active"}]


def test_marker_is_stable_across_runs():
    assert idempotency.comment_marker("4348411", MEMBERS) == \
           idempotency.comment_marker("4348411", list(reversed(MEMBERS)))


def test_marker_differs_per_work_item():
    assert idempotency.comment_marker("4348411", MEMBERS) != \
           idempotency.comment_marker("4348690", MEMBERS)


def test_marker_changes_when_a_user_is_added():
    extra = MEMBERS + [{"userid": "EF11111", "status": "created/active"}]
    assert idempotency.comment_marker("4348411", MEMBERS) != \
           idempotency.comment_marker("4348411", extra)


def test_marker_changes_when_an_outcome_changes():
    """"Already present" becoming "added" is a different statement and may post."""
    changed = [{"userid": "AB12345", "status": "already_active/active"},
               {"userid": "CD67890", "status": "already_active/active"}]
    assert idempotency.comment_marker("4348411", MEMBERS) != \
           idempotency.comment_marker("4348411", changed)


def test_second_run_recognises_its_own_comment():
    marker = idempotency.comment_marker("4348411", MEMBERS)
    existing = ["Some unrelated comment", f"ALM access provisioning result : ... {marker}"]
    assert idempotency.already_commented(existing, marker)


def test_rewrapped_rich_text_is_still_recognised():
    """Jazz stores comments as rich text and may re-wrap the whitespace."""
    marker = idempotency.comment_marker("4348411", MEMBERS)
    existing = [f"ALM access provisioning result :<br/>AB12345: Ada\n\n   {marker}   "]
    assert idempotency.already_commented(existing, marker)


def test_a_different_marker_does_not_block_a_post():
    assert not idempotency.already_commented(
        [f"old {idempotency.comment_marker('4348690', MEMBERS)}"],
        idempotency.comment_marker("4348411", MEMBERS))


def test_no_existing_comments_permits_a_post():
    assert not idempotency.already_commented([], idempotency.comment_marker("1", MEMBERS))
    assert not idempotency.already_commented(None, idempotency.comment_marker("1", MEMBERS))


def test_previous_markers_are_extractable_for_reporting():
    marker = idempotency.comment_marker("4348411", MEMBERS)
    assert idempotency.previous_markers([f"text {marker} more"]) == [marker]


def test_attachment_is_matched_by_filename_not_url():
    assert idempotency.already_attached("AB12345", ["AB12345.png"])
    assert idempotency.already_attached("AB12345", ["ab12345.PNG"])
    assert not idempotency.already_attached("AB12345", ["CD67890.png"])
    assert not idempotency.already_attached("AB12345", [])


def test_attachment_name_is_the_canonical_evidence_filename():
    assert idempotency.attachment_name("AB12345") == "AB12345.png"
