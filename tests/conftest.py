"""Test fixtures.

The scripts live in src/ and import each other by bare module name (they are run
as ``python src/<script>.py``), so src/ goes on sys.path rather than the package
being importable as ``alm.*``.

Every test runs with the ALM_/EWM_/JTS_ environment cleared. Without this a
developer's real .env decides whether alm_env() says PROD, and a test suite that
behaves differently on someone's machine is worse than none.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Neutralise ambient configuration for every test."""
    for key in list(os.environ):
        if key.startswith(("ALM_", "EWM_", "JTS_", "GPT_")) or key in {"CID", "COMMIT",
                                                                      "USER_IDS", "GROUP_NAME"}:
            monkeypatch.delenv(key, raising=False)
    # Deterministic run id so audit files land somewhere predictable.
    monkeypatch.setenv("ALM_RUN_ID", "TESTRUN")
    monkeypatch.setenv("ALM_TLS_VERIFY", "true")
    yield


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run inside a scratch directory: audit/state files are written relative to cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def users():
    """A representative retrieval result: two work items, one shared user."""
    return [
        {"userid": "AB12345", "email": "ada.lovelace@example.com",
         "first_name": "Ada", "last_name": "Lovelace",
         "source_work_items": [{"work_item_id": "4348411", "summary": "Grant ALM access"}]},
        {"userid": "CD67890", "email": "grace.hopper@example.com",
         "first_name": "Grace", "last_name": "Hopper",
         "source_work_items": [{"work_item_id": "4348411", "summary": "Grant ALM access"},
                               {"work_item_id": "4348690", "summary": "Second request"}]},
    ]
