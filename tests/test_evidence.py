"""Regression tests for the login-page-as-evidence incident (2026-08-27).

17 screenshots were captured, all of them the JTS login page, collapsing to two
distinct images. Every one was attached to a production work item and the run
reported SUCCESS 11/11. The invariant below - N users must produce N distinct
artifacts - is three lines and would have stopped it.
"""
from __future__ import annotations

import evidence


def write(path, payload: bytes):
    path.write_bytes(payload)
    return str(path)


def distinct_png(index: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes([index]) + b"x" * evidence.MIN_BYTES


def test_distinct_artifacts_pass(tmp_path):
    shots = {f"USER{i}": write(tmp_path / f"USER{i}.png", distinct_png(i)) for i in range(4)}
    ok, problems = evidence.validate(shots)
    assert ok, problems


def test_identical_artifacts_are_rejected(tmp_path):
    """The exact production signature: every file byte-identical."""
    same = distinct_png(1)
    shots = {f"USER{i}": write(tmp_path / f"USER{i}.png", same) for i in range(11)}
    ok, problems = evidence.validate(shots)
    assert not ok
    assert any("identical artifact shared by 11 users" in p for p in problems)


def test_two_distinct_images_across_seventeen_users_is_still_rejected(tmp_path):
    """The real batch had 7 of one image and 10 of another."""
    shots = {}
    for i in range(7):
        shots[f"A{i}"] = write(tmp_path / f"A{i}.png", distinct_png(1))
    for i in range(10):
        shots[f"B{i}"] = write(tmp_path / f"B{i}.png", distinct_png(2))
    ok, problems = evidence.validate(shots)
    assert not ok
    assert len(problems) == 2  # one per duplicated image


def test_missing_artifact_is_a_problem(tmp_path):
    ok, problems = evidence.validate({"AB12345": str(tmp_path / "nope.png")})
    assert not ok
    assert "no artifact" in problems[0]


def test_truncated_artifact_is_a_problem(tmp_path):
    ok, problems = evidence.validate({"AB12345": write(tmp_path / "small.png", b"tiny")})
    assert not ok
    assert "not a real profile page" in problems[0]


def test_empty_set_is_vacuously_valid():
    ok, problems = evidence.validate({})
    assert ok and problems == []


def test_duplicate_groups_reports_which_users_share_a_file(tmp_path):
    same = distinct_png(9)
    shots = {"AB12345": write(tmp_path / "a.png", same),
             "CD67890": write(tmp_path / "b.png", same),
             "EF11111": write(tmp_path / "c.png", distinct_png(3))}
    groups = evidence.duplicate_groups(shots)
    assert len(groups) == 1
    assert groups[0][1] == ["AB12345", "CD67890"]


def test_digest_is_content_addressed(tmp_path):
    a = write(tmp_path / "a.png", distinct_png(1))
    b = write(tmp_path / "b.png", distinct_png(1))
    assert evidence.file_digest(a) == evidence.file_digest(b)
