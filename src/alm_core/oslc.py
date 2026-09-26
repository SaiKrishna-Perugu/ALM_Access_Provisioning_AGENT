"""OSLC helpers: defensive XML parsing, pagination, and the New Users parser.

Consolidated from the CLI scripts, where the same XML sanitising and the same
paging loop existed in two places with slightly different bug fixes in each.

The parsing behaviour is deliberately identical to the CLI's, including the
quirks that were discovered the hard way: this EWM deployment returns XML with
invalid control characters and unescaped ampersands, and answers with an HTML
login page (HTTP 200) when a session is not authorised for API access.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterator
from typing import Any

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from .errors import AuthorizationError, DataError, ParseError
from .models import RequestedUser, SourceWorkItem

# Characters XML forbids that this server nonetheless emits. Built from code
# points rather than written as literals so the source stays pure ASCII and
# the intent survives a copy/paste through a tool that eats escape sequences.
_XML_LEGAL_RANGES = [
    (0x09, 0x0A), (0x0D, 0x0D), (0x20, 0xD7FF), (0xE000, 0xFFFD),
    (0x10000, 0x10FFFF),
]
_XML_INVALID_CHARS = re.compile(
    "[^" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _XML_LEGAL_RANGES) + "]")
_XML_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)")

RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"

# Custom attribute ids on this estate.
A_FCA = "rtc_ext:com.fca.alm.rtc.almAccessRequestOverview."
F_ACCESS_TYPE = A_FCA + "chnType"
F_NEW_USERS_LEGACY = A_FCA + "newUs"
F_NEW_USERS = "rtc_ext:com.stellantis.alm.rtc.aar.newUsers"
F_EXISTING_LEGACY = A_FCA + "existingUsers"
F_EXISTING = "rtc_ext:com.stellantis.alm.rtc.aar.existingUsers"
F_DOMAIN = A_FCA + "domain"
F_WORKAREAS = A_FCA + "workAreas"
F_JUSTIFICATION = A_FCA + "justification"

OSLC_PROPERTIES = ",".join([
    "dcterms:identifier", "dcterms:title", "dcterms:type", "rtc_cm:state",
    "oslc_cm:status", "dcterms:creator", "rtc_cm:modifiedBy", "dcterms:created",
    "dcterms:modified", F_ACCESS_TYPE, F_NEW_USERS_LEGACY, F_NEW_USERS,
    F_EXISTING_LEGACY, F_EXISTING, F_DOMAIN, F_WORKAREAS, F_JUSTIFICATION,
    "rtc_cm:plannedFor", "oslc_cm:approved",
])

OSLC_HEADERS_JSON = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}
OSLC_HEADERS_XML = {"Accept": "application/xml", "OSLC-Core-Version": "2.0"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml(content: bytes | str) -> ET.Element:
    """Parse OSLC XML, sanitising the malformations this server produces."""
    text = content.decode("utf-8", "replace") if isinstance(content, bytes | bytearray) \
        else content
    head = text.lstrip()[:64].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        raise AuthorizationError(
            "server returned the Jazz web UI, not OSLC XML - this session is not "
            "authorised for API access on this server")
    try:
        return ET.fromstring(text)
    except (ET.ParseError, DefusedXmlException) as err:
        if isinstance(err, DefusedXmlException):
            raise ParseError(f"hostile XML payload rejected: {err}") from err
        cleaned = _XML_BARE_AMP.sub("&amp;", _XML_INVALID_CHARS.sub("", text))
        try:
            return ET.fromstring(cleaned)
        except (ET.ParseError, DefusedXmlException) as err2:
            raise ParseError(f"unparseable OSLC XML: {err2}") from err2


def strip_html(text: str) -> str:
    """Rich text to a readable single line."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "; ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip(" ;")


def resource_uri(value: Any) -> str:
    return value.get("rdf:resource", "") if isinstance(value, dict) else ""


def as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def tail(uri: str, sep: str = "/") -> str:
    return uri.rsplit(sep, 1)[-1] if uri else ""


# ------------------------------------------------------------------ pagination

def iter_pages(session, url: str, params: dict, *, timeout, verify,
               max_pages: int = 100) -> Iterator[dict]:
    """Yield each OSLC result page, following oslc:nextPage.

    ``max_pages`` is a guard, not a limit: an OSLC server that returns a
    nextPage pointing at itself would otherwise spin forever.
    """
    next_url: str | None = url
    page = 0
    while next_url and page < max_pages:
        page += 1
        response = session.get(next_url, headers=OSLC_HEADERS_JSON,
                               params=params if page == 1 else None,
                               timeout=timeout, verify=verify)
        if response.status_code != 200:
            raise DataError(
                f"OSLC query failed: HTTP {response.status_code}",
                context={"url": next_url, "body": response.text[:300]})
        try:
            data = response.json()
        except ValueError as err:
            raise ParseError(f"OSLC response was not JSON: {err}") from err
        yield data

        info = data.get("oslc:responseInfo", {})
        nxt = data.get("oslc:nextPage") or (
            info.get("oslc:nextPage") if isinstance(info, dict) else None)
        if isinstance(nxt, dict):
            nxt = nxt.get("rdf:resource")
        next_url = nxt if nxt and nxt != next_url else None


def collect_results(session, url: str, params: dict, *, timeout, verify,
                    limit: int | None = None) -> list[dict]:
    items: list[dict] = []
    for data in iter_pages(session, url, params, timeout=timeout, verify=verify):
        items.extend(data.get("oslc:results", []))
        if limit and len(items) >= limit:
            return items[:limit]
    return items


# -------------------------------------------------------------- New Users field

def parse_new_users(field: str) -> tuple[list[dict], list[str]]:
    """Parse ``LASTNAME,FIRSTNAME,email,USERID;`` rows.

    Returns ``(records, rejected)``. The CLI dropped unparseable rows silently,
    which meant a malformed entry produced a user who never got access and no
    signal anywhere. Rejected rows are returned so the caller can route them to
    the LLM fallback and, failing that, to a human.
    """
    records: list[dict] = []
    rejected: list[str] = []
    if not field or field.lstrip().startswith("(from "):
        return records, rejected

    for entry in field.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(",")]
        if len(parts) < 4 or "@" not in parts[-2] or not parts[-1]:
            rejected.append(entry)
            continue
        records.append({
            "userid": parts[-1],
            "email": parts[-2],
            "first_name": ",".join(parts[1:-2]).strip(),
            "last_name": parts[0],
        })
    return records, rejected


def users_from_workitem(row: dict, work_item_id: str) -> tuple[list[RequestedUser], list[str]]:
    """Turn one work-item row into validated RequestedUser models.

    A record that the deterministic parser produced but the model rejects (an
    impossible user ID, say) is reported as rejected rather than dropped.
    """
    field = (row.get(F_NEW_USERS) or row.get(F_NEW_USERS_LEGACY) or "").strip()
    records, rejected = parse_new_users(field)
    source = SourceWorkItem(
        work_item_id=work_item_id,
        summary=(row.get("dcterms:title") or "").strip(),
        domain=tail(resource_uri(row.get(F_DOMAIN)), "."),
    )
    users: list[RequestedUser] = []
    for record in records:
        try:
            users.append(RequestedUser(**record, source_work_items=[source]))
        except Exception:  # pydantic ValidationError
            rejected.append(",".join(str(v) for v in record.values()))
    return users, rejected


def merge_users(batches: list[list[RequestedUser]]) -> list[RequestedUser]:
    """Deduplicate by user ID across work items, keeping every source."""
    merged: dict[str, RequestedUser] = {}
    for batch in batches:
        for user in batch:
            existing = merged.get(user.userid)
            if existing is None:
                merged[user.userid] = user.model_copy(deep=True)
                continue
            known = {s.work_item_id for s in existing.source_work_items}
            for source in user.source_work_items:
                if source.work_item_id not in known:
                    existing.source_work_items.append(source)
    return [merged[uid] for uid in sorted(merged)]
