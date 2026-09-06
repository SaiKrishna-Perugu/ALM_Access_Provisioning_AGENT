"""Checkpoint handling and the approval gate as the orchestrator applies them.

The state file used to carry no schema and no environment, so a format change
broke --resume silently and a run could be resumed into the other environment -
which, with the .env server line being toggled by hand several times a day, is
how a TEST run ends up commenting on production work items.
"""
from __future__ import annotations

import json

import pytest

import audit
import plan_lock
import run_pipeline as rp


def write_state(payload: dict) -> None:
    import os
    os.makedirs("out", exist_ok=True)
    with open(rp.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def write_users(users: list[dict], path: str = "out/alm_users.json") -> str:
    import os
    os.makedirs("out", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"users": users}, fh)
    return path


USERS = [
    {"userid": "AB12345", "email": "ada@example.com", "first_name": "Ada",
     "last_name": "Lovelace",
     "source_work_items": [{"work_item_id": "4348411"}]},
    {"userid": "CD67890", "email": "grace@example.com", "first_name": "Grace",
     "last_name": "Hopper",
     "source_work_items": [{"work_item_id": "4348690"}]},
]


# ------------------------------------------------------------------ state

def test_a_fresh_run_ignores_any_existing_state(workdir):
    write_state({"schema": 1, "env": "TEST", "steps": {"import": {"done": True}}})
    state = rp.load_state(resume=False)
    assert state["steps"] == {}
    assert state["plan"] is None


def test_resume_reads_the_checkpoint(workdir, monkeypatch):
    monkeypatch.setenv("ALM_ENV", "TEST")
    write_state({"schema": 1, "env": "TEST", "steps": {"import": {"done": True}},
                 "verified": {"AB12345": True}})
    state = rp.load_state(resume=True)
    assert state["steps"]["import"]["done"]
    assert state["verified"] == {"AB12345": True}


def test_resume_refuses_a_different_environment(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "PROD")
    write_state({"schema": 1, "env": "TEST", "steps": {}})
    with pytest.raises(SystemExit):
        rp.load_state(resume=True)
    assert "Refusing to resume across environments" in capsys.readouterr().out


def test_resume_refuses_an_older_schema(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "TEST")
    write_state({"env": "TEST", "steps": {}})  # schema 0 / absent
    with pytest.raises(SystemExit):
        rp.load_state(resume=True)
    assert "schema" in capsys.readouterr().out


def test_resume_refuses_a_corrupt_state_file(workdir, monkeypatch, capsys):
    import os
    os.makedirs("out", exist_ok=True)
    with open(rp.STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    with pytest.raises(SystemExit):
        rp.load_state(resume=True)
    assert "could not be read" in capsys.readouterr().out


def test_resume_with_no_state_file_starts_clean(workdir):
    assert rp.load_state(resume=True)["steps"] == {}


def test_saved_state_carries_schema_and_environment(workdir, monkeypatch):
    monkeypatch.setenv("ALM_ENV", "TEST")
    rp.save_state({"steps": {}, "verified": {}})
    with open(rp.STATE_PATH, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["schema"] == 1
    assert saved["env"] == "TEST"
    assert "updated" in saved


# ------------------------------------------------------------ approval gate

def test_dry_run_records_the_plan(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "TEST")
    users_in = write_users(USERS)
    state = rp.load_state(resume=False)

    plan = rp.resolve_plan(users_in, state, commit=False, force_replan=False)

    assert plan["user_count"] == 2
    assert state["plan"]["hash"] == plan["hash"]
    with open(rp.STATE_PATH, encoding="utf-8") as fh:
        assert json.load(fh)["plan"]["hash"] == plan["hash"]
    assert "Approve this plan" in capsys.readouterr().out


def test_commit_proceeds_when_the_plan_is_unchanged(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "TEST")
    users_in = write_users(USERS)
    state = rp.load_state(resume=False)
    rp.resolve_plan(users_in, state, commit=False, force_replan=False)

    plan = rp.resolve_plan(users_in, state, commit=True, force_replan=False)
    assert plan is not None
    assert "matches the approved dry run" in capsys.readouterr().out


def test_commit_aborts_when_a_user_appeared_after_approval(workdir, monkeypatch, capsys):
    """The exact production scenario: the queue grew between review and commit."""
    monkeypatch.setenv("ALM_ENV", "TEST")
    users_in = write_users(USERS[:1])
    state = rp.load_state(resume=False)
    rp.resolve_plan(users_in, state, commit=False, force_replan=False)

    write_users(USERS, users_in)  # a second request lands in the live queue
    assert rp.resolve_plan(users_in, state, commit=True, force_replan=False) is None

    out = capsys.readouterr().out
    assert "APPROVAL GATE" in out
    assert "CD67890" in out
    assert "4348690" in out


def test_commit_aborts_when_no_dry_run_was_reviewed(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "TEST")
    users_in = write_users(USERS)
    state = rp.load_state(resume=False)
    assert rp.resolve_plan(users_in, state, commit=True, force_replan=False) is None
    assert "No approved plan on file" in capsys.readouterr().out


def test_force_replan_overrides_the_gate_loudly(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "TEST")
    users_in = write_users(USERS)
    state = rp.load_state(resume=False)
    plan = rp.resolve_plan(users_in, state, commit=True, force_replan=True)
    assert plan is not None
    assert "--force-replan" in capsys.readouterr().out


# --------------------------------------------------------- verified handoff

def test_write_verified_keeps_only_verified_users_and_stamps_the_run(workdir, monkeypatch):
    monkeypatch.setenv("ALM_ENV", "TEST")
    users_in = write_users(USERS)
    plan = plan_lock.plan_summary(USERS)

    count = rp.write_verified(users_in, {"AB12345": True, "CD67890": False}, plan)

    assert count == 1
    with open(rp.VERIFIED_PATH, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert [u["userid"] for u in payload["users"]] == ["AB12345"]
    assert payload["run_id"] == audit.run_id()
    assert payload["env"] == "TEST"
    assert payload["plan_hash"] == plan["hash"]


def test_write_verified_with_nobody_verified_writes_an_empty_set(workdir):
    users_in = write_users(USERS)
    assert rp.write_verified(users_in, {}, None) == 0


# ------------------------------------------------------------- step results

def test_run_step_accepts_only_the_declared_exit_codes(workdir, monkeypatch):
    monkeypatch.setattr(rp.subprocess, "call", lambda _cmd: 3)
    state = {"steps": {}}
    assert not rp.run_step("comment", ["python", "x.py"], state)
    assert rp.run_step("import", ["python", "x.py"], state, ok_codes=(0, 3))
    assert state["steps"]["import"]["rc"] == 3


def test_run_step_records_the_exit_code_even_on_failure(workdir, monkeypatch):
    monkeypatch.setattr(rp.subprocess, "call", lambda _cmd: 2)
    state = {"steps": {}}
    assert not rp.run_step("gpt", ["python", "x.py"], state)
    assert state["steps"]["gpt"] == {"done": False, "rc": 2,
                                     "ts": state["steps"]["gpt"]["ts"]}
