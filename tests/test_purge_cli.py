"""purge() and the CLI's out/ files: screenshots, user caches and comment capture go;
the CLI's out/audit records stay (they are the record of what was written)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("aiosqlite")
pytest.importorskip("langgraph.checkpoint.sqlite")

from alm_agents.local import purge  # noqa: E402
from alm_agents.runner import Console  # noqa: E402
from alm_core.config import Settings  # noqa: E402


class Events(Console):
    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind, data):
        self.events.append((kind, data))


def test_purge_cli_artifacts(tmp_path):
    out = tmp_path / "out"
    local_dir = out / "local"
    local_dir.mkdir(parents=True)

    # 1. CLI audit folder
    audit_dir = out / "audit"
    audit_dir.mkdir(parents=True)
    old_audit = audit_dir / "audit-old.json"
    old_audit.write_text('{"user": "old"}', encoding="utf-8")
    new_audit = audit_dir / "audit-new.json"
    new_audit.write_text('{"user": "new"}', encoding="utf-8")

    # 2. CLI screenshots folder
    shots_dir = out / "screenshots"
    shots_dir.mkdir(parents=True)
    old_shot = shots_dir / "old_profile.png"
    old_shot.write_bytes(b"png-old")
    new_shot = shots_dir / "new_profile.png"
    new_shot.write_bytes(b"png-new")

    # 3. CLI user cache files
    old_users = out / "alm_users.json"
    old_users.write_text('{"users": []}', encoding="utf-8")
    old_users_verified = out / "alm_users_verified.json"
    old_users_verified.write_text('{"users": []}', encoding="utf-8")

    # 4. CLI comment capture file
    old_comments = out / "comment_capture.json"
    old_comments.write_text("{}", encoding="utf-8")

    now = datetime.now(timezone.utc)
    old_stamp = (now - timedelta(days=45)).timestamp()
    new_stamp = (now - timedelta(days=5)).timestamp()

    # Backdate older files
    for p in (old_audit, old_shot, old_users, old_users_verified, old_comments):
        os.utime(p, (old_stamp, old_stamp))

    # Keep new files recent
    for p in (new_audit, new_shot):
        os.utime(p, (new_stamp, new_stamp))

    settings = Settings(_env_file=None, environment="TEST",
                        ledger_path=str(local_dir / "alm.db"))

    # A dry run reports the same counts and deletes nothing.
    preview = asyncio.run(purge(settings, Events(), 30, out_dir=local_dir, now=now,
                                dry_run=True))
    assert all(p.exists() for p in (old_audit, old_shot, old_users, old_comments))

    # Purge older than 30 days
    purged = asyncio.run(purge(settings, Events(), 30, out_dir=local_dir, now=now))
    assert purged == preview

    # Check counts
    assert "cli_audit" not in purged
    assert purged["cli_screenshots"] == 1
    assert purged["cli_users"] == 2
    assert purged["cli_comments"] == 1

    # The CLI's audit records are kept, however old.
    assert old_audit.exists()

    # Old working files removed
    assert not old_shot.exists()
    assert not old_users.exists()
    assert not old_users_verified.exists()
    assert not old_comments.exists()

    # Recent files still present
    assert new_audit.exists()
    assert new_shot.exists()
