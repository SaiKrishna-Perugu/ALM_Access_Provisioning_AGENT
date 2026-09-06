"""The commit guard must cover every write entry point.

It used to match ``jts_import_users`` only, so the documented primary entry
point -- ``run_pipeline.py --commit`` -- and the GPT, comment and attach steps
could all write with no pre-flight check at all.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / ".github" / "hooks" / "scripts" / "guard-jts-commit.py"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("guard_jts_commit", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def populated(workdir):
    Path("out").mkdir(exist_ok=True)
    Path("out/alm_users.json").write_text(
        json.dumps({"users": [{"userid": "AB12345"}]}), encoding="utf-8")
    return workdir


@pytest.fixture
def approved_plan(populated):
    Path("out/pipeline_state.json").write_text(
        json.dumps({"schema": 1, "env": "TEST", "steps": {},
                    "plan": {"hash": "a" * 64, "user_count": 1, "work_item_count": 1}}),
        encoding="utf-8")
    return populated


WRITE_SCRIPTS = ["jts_import_users", "elm_gpt", "ewm_comment_workitems", "jts_profile_attach"]


def test_non_commit_commands_are_untouched(guard, workdir):
    for cmd in ("python src/jts_import_users.py",
                "python src/run_pipeline.py",
                "git status",
                "python src/alm_access_requests.py --all-open"):
        assert guard.evaluate(cmd)[0], cmd


@pytest.mark.parametrize("script", WRITE_SCRIPTS)
def test_every_write_script_is_blocked_without_users(guard, workdir, script):
    allowed, reason = guard.evaluate(f"python src/{script}.py --commit")
    assert not allowed
    assert "does not exist" in reason


@pytest.mark.parametrize("script", WRITE_SCRIPTS)
def test_every_write_script_is_allowed_with_users(guard, populated, script):
    assert guard.evaluate(f"python src/{script}.py --commit")[0]


def test_empty_user_file_is_blocked(guard, workdir):
    Path("out").mkdir(exist_ok=True)
    Path("out/alm_users.json").write_text(json.dumps({"users": []}), encoding="utf-8")
    allowed, reason = guard.evaluate("python src/jts_import_users.py --commit")
    assert not allowed
    assert "contains no users" in reason


def test_unreadable_user_file_is_blocked(guard, workdir):
    Path("out").mkdir(exist_ok=True)
    Path("out/alm_users.json").write_text("{not json", encoding="utf-8")
    allowed, reason = guard.evaluate("python src/jts_import_users.py --commit")
    assert not allowed
    assert "could not be read as JSON" in reason


def test_commit_via_environment_variable_is_caught(guard, workdir):
    assert not guard.evaluate("COMMIT=true python src/jts_import_users.py")[0]


def test_users_in_override_is_honoured(guard, workdir, tmp_path):
    other = tmp_path / "verified.json"
    other.write_text(json.dumps({"users": [{"userid": "CD67890"}]}), encoding="utf-8")
    assert guard.evaluate(
        f'python src/ewm_comment_workitems.py --users-in "{other}" --commit')[0]


def test_pipeline_commit_is_blocked_without_an_approved_dry_run(guard, populated):
    allowed, reason = guard.evaluate("python src/run_pipeline.py --commit")
    assert not allowed
    assert "no dry run has been reviewed" in reason


def test_pipeline_commit_is_allowed_after_a_dry_run(guard, approved_plan):
    allowed, reason = guard.evaluate("python src/run_pipeline.py --commit")
    assert allowed
    assert "approved plan" in reason


def test_pipeline_state_without_a_plan_is_blocked(guard, populated):
    Path("out/pipeline_state.json").write_text(
        json.dumps({"schema": 1, "steps": {}}), encoding="utf-8")
    allowed, reason = guard.evaluate("python src/run_pipeline.py --commit")
    assert not allowed
    assert "no approved plan" in reason


def test_pipeline_skip_retrieve_checks_the_user_file_instead(guard, populated):
    assert guard.evaluate("python src/run_pipeline.py --commit --skip-retrieve")[0]


def test_pipeline_skip_retrieve_without_users_is_blocked(guard, workdir):
    assert not guard.evaluate("python src/run_pipeline.py --commit --skip-retrieve")[0]


def test_force_replan_is_an_explicit_override(guard, workdir):
    allowed, reason = guard.evaluate("python src/run_pipeline.py --commit --force-replan")
    assert allowed
    assert "explicitly overrides" in reason


def test_unarchive_names_its_own_target_and_is_allowed(guard, workdir):
    assert guard.evaluate("python src/jts_unarchive_user.py AB12345 --commit")[0]


def test_unrelated_commit_flags_are_not_intercepted(guard, workdir):
    assert guard.evaluate("git commit -m 'wip'")[0]


def test_windows_paths_and_venv_python_are_recognised(guard, workdir):
    allowed, _ = guard.evaluate(
        r'.\.venv\Scripts\python.exe src\jts_import_users.py --commit')
    assert not allowed, "backslash paths must still be matched"


def test_deny_payload_shape(guard, workdir, capsys):
    guard._deny("because")
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
