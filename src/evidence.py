"""Validate evidence artifacts before they are attached to a work item.

On 2026-08-27 this pipeline attached the JTS *login page* to 11 production work
items as proof of provisioning, and reported SUCCESS 11/11. The browser step had
verified its login using the same expression the login step used to decide
whether to log in at all, so both agreed and both were wrong.

The cheap invariant that would have caught it: N users must produce N distinct
screenshots. All 17 files were byte-identical in pairs -- two distinct images
across the whole batch. This module is that invariant, as a hard gate.

It is deliberately independent of how the screenshot was taken. The capture step
proves the *content* (the profile page contains the user's ID in an input
value); this proves the *set* (no two users share an artifact). Two unrelated
signals have to agree before anything is uploaded.
"""
from __future__ import annotations

import hashlib
import os

# A full-page PNG of a real profile is tens of KB. Anything this small is an
# error page or a blank viewport, whatever its filename says.
MIN_BYTES = 4096


def file_digest(path: str) -> str:
    """sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def duplicate_groups(shots: dict[str, str]) -> list[tuple[str, list[str]]]:
    """[(digest, [userids sharing it])] for every artifact used more than once."""
    by_digest: dict[str, list[str]] = {}
    for uid in sorted(shots):
        path = shots[uid]
        if not path or not os.path.exists(path):
            continue
        by_digest.setdefault(file_digest(path), []).append(uid)
    return [(digest, uids) for digest, uids in sorted(by_digest.items())
            if len(uids) > 1]


def validate(shots: dict[str, str], min_bytes: int = MIN_BYTES) -> tuple[bool, list[str]]:
    """Check an artifact set is fit to upload.

    Returns (ok, problems). ``ok=False`` must abort the attach step for the whole
    batch rather than skipping individual users: identical artifacts mean the
    capture mechanism itself is broken, so the remaining files cannot be trusted
    either, however plausible they look.
    """
    problems: list[str] = []
    for uid in sorted(shots):
        path = shots[uid]
        if not path or not os.path.exists(path):
            problems.append(f"{uid}: no artifact at {path or '(unset)'}")
            continue
        size = os.path.getsize(path)
        if size < min_bytes:
            problems.append(f"{uid}: artifact is only {size} bytes "
                            f"(minimum {min_bytes}) - not a real profile page")

    for digest, uids in duplicate_groups(shots):
        problems.append(
            f"identical artifact shared by {len(uids)} users ({', '.join(uids)}) "
            f"[sha256 {digest[:12]}] - this is the signature of the "
            "login-page-as-evidence defect; nothing will be uploaded")

    return not problems, problems
