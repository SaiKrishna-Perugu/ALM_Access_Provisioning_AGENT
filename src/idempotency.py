"""Make the write steps safe to re-run.

Neither writing step used to check whether it had already run. A repeated
``--commit`` posted the same comment again and uploaded the same screenshot
again, and ``link_attachment``'s "already linked" guard never fired because a
re-upload produces a fresh attachment URL every time. That turned the ordinary
recovery action -- re-running after a partial failure -- into a way to corrupt
the record. It is not hypothetical: the 2026-09-01 run posted 13 of 17 comments
and then died on a connection reset, leaving the only route forward a re-run
that would have doubled those 13.

Each comment now carries a marker derived from its own content, so a re-run can
recognise its previous post; each screenshot is matched against the attachment
titles already on the work item. Both are pure functions here so they can be
tested without a Jazz server.
"""
from __future__ import annotations

import hashlib
import re

import alm_config

MARKER_RE = re.compile(
    rf"\[{re.escape(alm_config.COMMENT_MARKER_PREFIX)}:(?P<wid>[^:\]]+):(?P<digest>[0-9a-f]+)\]")


def comment_marker(workitem_id: str, members: list[dict]) -> str:
    """A stable marker for the comment this run would post on this work item.

    Derived from the work item plus each user's id and reported status, so:
      * re-running an unchanged batch produces the same marker -> skipped;
      * a genuinely different message (a new user, or a user whose outcome
        changed from "already present" to "added") produces a new marker and is
        allowed to post.
    """
    parts = sorted(f"{m.get('userid', '')}={m.get('status', '')}" for m in members)
    blob = f"{workitem_id}|" + "|".join(parts)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return f"[{alm_config.COMMENT_MARKER_PREFIX}:{workitem_id}:{digest}]"


def already_commented(existing_comments, marker: str) -> bool:
    """True when one of the work item's existing comments carries this marker.

    Whitespace is normalised first: Jazz stores comments as rich text and may
    re-wrap or entity-escape what was posted.
    """
    target = marker.strip()
    for text in existing_comments or []:
        if not text:
            continue
        flat = " ".join(str(text).split())
        if target in flat:
            return True
    return False


def previous_markers(existing_comments) -> list[str]:
    """Every alm-agent marker already present, for reporting what was found."""
    found: list[str] = []
    for text in existing_comments or []:
        for match in MARKER_RE.finditer(" ".join(str(text or "").split())):
            if match.group(0) not in found:
                found.append(match.group(0))
    return found


# A comment line about one user: "AB12345: NAME: User added to JTS - (active)".
# Both the CLI and the agents write this shape, with or without a marker.
_USER_LINE = re.compile(r"^\s*([A-Za-z]{1,3}[0-9][0-9A-Za-z]{3,8})\s*:(.*)$")


def _comment_lines(comment) -> list[str]:
    """A stored comment (EWM keeps HTML) as plain text lines."""
    import html

    text = re.sub(r"<br\s*/?>", "\n", str(comment or ""), flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text.splitlines()


def already_reported(existing_comments, userid: str, action_text: str) -> bool:
    """True when some comment already states this outcome for this user.

    Recognises the other tool's comments too: the agents' comments carry no
    marker, and a CLI comment's marker means nothing to the agents, so the
    shared ground is the per-user line itself.
    """
    want_user, want_text = userid.upper(), " ".join(action_text.split()).lower()
    for comment in existing_comments or []:
        for line in _comment_lines(comment):
            match = _USER_LINE.match(line)
            if match and match.group(1).upper() == want_user and \
                    want_text in " ".join(match.group(2).split()).lower():
                return True
    return False


def attachment_name(userid: str) -> str:
    """The canonical evidence filename for a user."""
    return f"{userid}.png"


def already_attached(userid: str, existing_titles) -> bool:
    """True when this user's evidence file is already on the work item."""
    wanted = attachment_name(userid).lower()
    return any((title or "").strip().lower() == wanted for title in existing_titles or [])
