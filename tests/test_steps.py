"""Step-level logic that does not need a Jazz server: GPT parsing, poll arithmetic."""
from __future__ import annotations

import elm_gpt
import jts_permission as perm

# ------------------------------------------------------------------ GPT

def test_gpt_reports_success_when_it_says_zero_failed():
    """The real page text GPT returns after a successful Modify."""
    ok, msg = elm_gpt.parse_submit_body(
        "Request Summary Submitted Requests: 10 Failed Requests: 0")
    assert ok
    assert "Failed Requests: 0" in msg


def test_gpt_reports_failure_when_requests_failed():
    ok, _ = elm_gpt.parse_submit_body("Submitted Requests: 7 Failed Requests: 3")
    assert not ok


def test_gpt_accepts_the_prose_confirmation():
    assert elm_gpt.parse_submit_body("Your request has been submitted correctly.")[0]


def test_gpt_failure_count_wins_over_prose():
    """A page that says both must be believed on the number, not the sentence."""
    ok, _ = elm_gpt.parse_submit_body("submitted correctly ... Failed Requests: 2")
    assert not ok


def test_gpt_silence_is_not_success():
    """The old check treated a missing 'Failed Requests' line as ambiguous; an
    empty page must never read as a successful submission."""
    assert not elm_gpt.parse_submit_body("")[0]
    assert not elm_gpt.parse_submit_body("Session expired. Please log in.")[0]


def test_gpt_message_is_truncated_for_the_audit():
    assert len(elm_gpt.parse_submit_body("x" * 500)[1]) <= 160


# ------------------------------------------------------- permission poll

def test_documented_poll_performs_seven_checks():
    """--wait 30 --interval 5 is t=0,5,10,15,20,25,30. The code and the report
    used to disagree about this number."""
    assert perm.max_attempts(30, 5) == 7


def test_a_dry_run_does_a_single_check():
    assert perm.max_attempts(0, 5) == 1


def test_interval_larger_than_the_cap_still_checks_once():
    assert perm.max_attempts(5, 30) == 1


def test_has_role_requires_the_role_and_an_active_account():
    assert perm.has_role({"userId": "AB12345", "roles": ["JazzUsers"], "archived": False})
    assert not perm.has_role({"userId": "AB12345", "roles": ["JazzUsers"], "archived": True})
    assert not perm.has_role({"userId": "AB12345", "roles": ["JazzProjectAdmins"]})
    assert not perm.has_role(None)


def test_has_role_treats_a_missing_archived_flag_as_active():
    assert perm.has_role({"userId": "AB12345", "roles": ["JazzUsers"]})


def test_poll_stops_as_soon_as_everyone_verifies(monkeypatch, workdir):
    calls = []

    def fake_details(_session, _server, uid):
        calls.append(uid)
        return {"userId": uid, "roles": ["JazzUsers"], "archived": False}

    monkeypatch.setattr(perm, "contributor_details", fake_details)
    verified, meta = perm.poll_roles(None, "https://jts", ["AB12345", "CD67890"], wait_min=30)
    assert all(verified.values())
    assert meta["attempts"] == 1, "no reason to wait once everyone is verified"
    assert sorted(calls) == ["AB12345", "CD67890"]


def fake_clock(monkeypatch):
    """Make sleep advance a virtual clock so a 30-minute poll runs instantly."""
    now = [1_000_000.0]
    monkeypatch.setattr(perm.time, "time", lambda: now[0])
    monkeypatch.setattr(perm.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_poll_only_rechecks_the_pending_users(monkeypatch, workdir):
    seen = []

    def fake_details(_session, _server, uid):
        seen.append(uid)
        return {"userId": uid, "roles": ["JazzUsers"] if uid == "AB12345" else [],
                "archived": False}

    monkeypatch.setattr(perm, "contributor_details", fake_details)
    fake_clock(monkeypatch)
    verified, meta = perm.poll_roles(None, "https://jts", ["AB12345", "CD67890"],
                                     wait_min=2, interval_min=1)
    assert verified == {"AB12345": True, "CD67890": False}
    assert seen.count("AB12345") == 1, "an already-verified user is not re-checked"
    assert seen.count("CD67890") > 1
    assert meta["attempts"] <= meta["max_attempts"]


def test_a_full_thirty_minute_poll_performs_the_documented_seven_checks(monkeypatch, workdir):
    """Behaviour must match max_attempts(), which the final report now prints."""
    monkeypatch.setattr(perm, "contributor_details",
                        lambda _s, _srv, uid: {"userId": uid, "roles": []})
    fake_clock(monkeypatch)
    _verified, meta = perm.poll_roles(None, "https://jts", ["AB12345"],
                                      wait_min=30, interval_min=5)
    assert meta["attempts"] == meta["max_attempts"] == perm.max_attempts(30, 5) == 7


def test_parallel_check_returns_the_same_answers(monkeypatch, workdir):
    monkeypatch.setattr(perm, "contributor_details",
                        lambda _s, _srv, uid: {"userId": uid, "roles": ["JazzUsers"]})
    verified, meta = perm.poll_roles(None, "https://jts", ["A", "B", "C", "D"], workers=4)
    assert all(verified.values())
    assert meta["workers"] == 4
