"""The audit must not manufacture confidence.

Two defects it used to have: a dry run printed "SUCCESS (17)" although nothing
happened, because skipped counted as success; and when the attach step aborted
on one user, that user's error was recorded against four others the run never
touched.
"""
from __future__ import annotations

import json

import audit


def test_dry_run_summary_never_claims_success(workdir):
    for uid in ("AB12345", "CD67890"):
        audit.record("import", uid, audit.SKIPPED, outcome="dry_run")
    audit.flush("import")

    table = audit.summary_table(audit.load_all(), dry_run=True)
    assert "SUCCESS" not in table.upper()
    assert "PLANNED (2)" in table
    assert "nothing was written" in table.lower()


def test_commit_summary_separates_the_statuses(workdir):
    audit.record("import", "AB12345", audit.OK, outcome="created")
    audit.record("import", "CD67890", audit.SKIPPED, outcome="already_active")
    audit.record("import", "EF11111", audit.FAILED, outcome="not_in_ldap")
    audit.record("import", "GH22222", audit.NOT_ATTEMPTED, outcome="not_attempted")
    audit.flush("import")

    table = audit.summary_table(audit.load_all())
    assert "SUCCEEDED     (1): AB12345" in table
    assert "SKIPPED       (1): CD67890" in table
    assert "NOT ATTEMPTED (1): GH22222" in table
    assert "FAILED        (1): EF11111" in table


def test_not_attempted_is_not_counted_as_a_failure(workdir):
    audit.record("attach", "AB12345", audit.FAILED, outcome="attach_failed", message="boom")
    audit.record_not_attempted("attach", ["CD67890", "EF11111"], "aborted before this user")
    audit.flush("attach")

    records = audit.load_all()
    failed = audit.failures(records)
    assert [r["userid"] for r in failed] == ["AB12345"]

    tally = audit.counts(records)
    assert tally == {"users": 3, "succeeded": 0, "skipped": 0, "not_attempted": 2,
                     "timed_out": 0, "failed": 1}


def test_not_attempted_users_do_not_inherit_another_users_error(workdir):
    audit.record("attach", "AB12345", audit.FAILED, outcome="attach_failed",
                 message="profile not confirmed")
    audit.record_not_attempted("attach", ["CD67890"], "step aborted")
    audit.flush("attach")

    rows = {r["userid"]: r for r in audit.load_all()}
    assert "profile not confirmed" not in rows["CD67890"]["message"]
    assert rows["CD67890"]["status"] == audit.NOT_ATTEMPTED


def test_worst_status_wins_per_step(workdir):
    audit.record("import", "AB12345", audit.OK, outcome="created")
    audit.record("import", "AB12345", audit.FAILED, outcome="verify_failed")
    audit.flush("import")
    assert audit.by_user(audit.load_all())["AB12345"]["import"] == audit.FAILED


def test_exception_is_captured_with_a_traceback(workdir):
    try:
        raise ValueError("connection reset")
    except ValueError as err:
        audit.record("comment", "AB12345", audit.FAILED, outcome="post_failed", exc=err)
    audit.flush("comment")
    row = audit.load_all()[0]
    assert row["error"] == "ValueError: connection reset"
    assert "ValueError" in row["traceback"]


def test_records_carry_schema_and_environment(workdir, monkeypatch):
    monkeypatch.setenv("ALM_ENV", "TEST")
    audit.record("import", "AB12345", audit.OK)
    audit.flush("import")
    row = audit.load_all()[0]
    assert row["schema"] == 1
    assert row["env"] == "TEST"


def test_aggregate_writes_a_run_file_with_metadata(workdir, monkeypatch):
    monkeypatch.setenv("ALM_ENV", "TEST")
    audit.record("verify", "AB12345", audit.OK)
    audit.flush("verify")
    path, records = audit.aggregate(extra={"mode": "commit"})
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["run_id"] == "TESTRUN"
    assert payload["env"] == "TEST"
    assert payload["mode"] == "commit"
    assert payload["count"] == len(records) == 1


def test_outcomes_for_step_returns_the_worst_record_per_user(workdir):
    audit.record("import", "AB12345", audit.OK, outcome="created")
    audit.record("import", "AB12345", audit.FAILED, outcome="create_failed")
    audit.flush("import")
    assert audit.outcomes_for_step("import")["AB12345"]["outcome"] == "create_failed"


def test_empty_run_says_so(workdir):
    assert audit.summary_table([]) == "No audit records for this run."
