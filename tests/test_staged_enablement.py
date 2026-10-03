"""Staged enablement and kill switches: a deployment performs only the write
operations it is told to, plans and shows the rest, and never fails a run
over them."""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytestmark = pytest.mark.usefixtures("scripted_recovery")

from test_agentic_sandbox import (  # noqa: E402
    FULL_PLAN,
    FULL_SCRIPTS,
    ScriptedLLM,
    approve,
    sandbox_settings,
)

from alm_agents.runner import Console  # noqa: E402
from alm_agents.sandbox import SandboxEstate, run_sandbox  # noqa: E402
from alm_core.config import Settings  # noqa: E402


def full_run(tmp_path, **settings):
    approve.calls = []
    fake = ScriptedLLM(FULL_PLAN, FULL_SCRIPTS)
    estate = SandboxEstate.default()
    report = asyncio.run(run_sandbox(
        sandbox_settings(**settings), llm=fake, supervisor_llm=fake, estate=estate,
        console=Console(), decide=approve, shots_dir=str(tmp_path / "shots")))
    return report, estate


def outcomes(report, operation):
    return {(r["userid"], r["outcome"]) for r in report["results"]
            if r["operation"] == operation}


def test_a_switched_off_operation_is_skipped_and_the_rest_still_happen(tmp_path):
    report, estate = full_run(tmp_path, writes_disabled_operations="ad_group_add")
    assert not report["halted"], report["halt_reason"]
    assert estate.ad_requests == []                                 # nothing reached AD
    skipped = [r for r in report["results"] if r["operation"] == "ad_group_add"]
    assert skipped and all(r["outcome"] == "skipped" for r in skipped)
    assert all("ALM_WRITES_DISABLED_OPERATIONS" in r["message"] for r in skipped)
    assert ("AB12345", "ok") in outcomes(report, "jts_create")     # JTS still written


def test_the_first_stage_writes_only_what_it_allows(tmp_path):
    """Stage one of the pilot: reactivations and comments, nothing new."""
    report, estate = full_run(
        tmp_path, allowed_operations="jts_unarchive,workitem_comment")
    assert ("CD67890", "ok") in outcomes(report, "jts_unarchive")
    assert outcomes(report, "jts_create") == {("AB12345", "skipped"), ("TB22322", "skipped")}
    assert estate.ad_requests == []
    assert not estate.people["AB12345"].contributor                 # not created
    # The comment reports what happened, so it never claims the skipped import.
    assert ("CD67890", "ok") in outcomes(report, "workitem_comment")
    posted = "\n".join(estate.comments["1001"])
    assert "User reactivated in JTS" in posted and "User added to JTS" not in posted


def test_an_unknown_operation_name_is_refused_at_start_up():
    with pytest.raises(ValueError, match="unknown operation"):
        Settings(_env_file=None, writes_disabled_operations="ad_group_remove")
    settings = Settings(_env_file=None, allowed_operations="jts_create",
                        writes_disabled_operations="jts_create")
    assert "ALM_WRITES_DISABLED_OPERATIONS" in settings.operation_blocked("jts_create")
    assert "ALM_ALLOWED_OPERATIONS" in settings.operation_blocked("ad_group_add")
    assert Settings(_env_file=None).operation_blocked("ad_group_add") == ""
