"""EWM tools: read the queue, comment on a work item, attach evidence.

Server quirks preserved verbatim from the CLI, because they were established
against this specific Jazz build and are not guesses:

* A POST to the bare comments collection returns 405; the ``oslc:comment`` child
  factory returns 201.
* Jazz rejects any POST without ``X-Jazz-CSRF-Prevent`` carrying the session's
  JSESSIONID value.
* The OSLC attachment factory returns 415 here, so uploads go through the web
  UI's ``IAttachmentRestService`` (multipart) followed by an OSLC partial PUT
  that links the attachment to the work item.
"""
from __future__ import annotations

import html
import json

from ..errors import DataError, NotFoundError, TransportError
from ..logging import get_logger
from ..models import Operation, ProvisionResult, WorkItem
from ..oslc import (
    F_ACCESS_TYPE,
    F_DOMAIN,
    F_JUSTIFICATION,
    F_NEW_USERS,
    F_NEW_USERS_LEGACY,
    F_WORKAREAS,
    OSLC_HEADERS_JSON,
    OSLC_HEADERS_XML,
    OSLC_PROPERTIES,
    collect_results,
    local_name,
    parse_xml,
    resource_uri,
    strip_html,
    tail,
    users_from_workitem,
)
from .base import ToolContext, guarded_write, to_thread

log = get_logger("alm.tools.ewm")

TYPE_ID = "com.fca.alm.rtc.workitem.workItemType.almAccessRequest"
ATTACH_PROP = "rtc_cm:com.ibm.team.workitem.linktype.attachment.attachment"
UPLOAD_PATH = "/service/com.ibm.team.workitem.service.internal.rest.IAttachmentRestService"

PROJECT_NAME = "Unified Tracking System (Change Management)"


def _server(ctx: ToolContext) -> str:
    return ctx.settings.ewm_server


def _csrf(ctx: ToolContext) -> dict:
    session = ctx.client.session(_server(ctx), kind="ewm")
    return {"X-Jazz-CSRF-Prevent": session.cookies.get("JSESSIONID") or ""}


# ------------------------------------------------------------------- discovery

def _project_uuid(ctx: ToolContext) -> str:
    """The configured project UUID, verified against this server before use.

    A UUID cached from TEST silently addresses nothing on PROD, so it is checked
    rather than trusted.
    """
    server = _server(ctx)
    configured = ctx.settings.ewm_project_uuid
    if configured:
        probe = ctx.client.request("GET", f"{server}/process/project-areas/{configured}",
                                   server=server, kind="ewm", headers=OSLC_HEADERS_XML)
        if probe.status_code == 200:
            return configured
        log.warning("project_uuid_invalid_here", uuid=configured, server=server)

    response = ctx.client.request("GET", f"{server}/process/project-areas",
                                  server=server, kind="ewm", headers=OSLC_HEADERS_XML)
    root = parse_xml(response.content)
    for area in root.iter():
        if local_name(area.tag) != "project-area":
            continue
        name = next((v for k, v in area.attrib.items() if local_name(k) == "name"), "")
        if name.strip() == PROJECT_NAME:
            for child in area:
                if local_name(child.tag) == "url" and child.text:
                    return child.text.rstrip("/").split("/")[-1]
    raise NotFoundError(f"project area not found: {PROJECT_NAME}")


# ----------------------------------------------------------------------- reads

def _fetch_queue(ctx: ToolContext, limit: int | None) -> list[WorkItem]:
    server = _server(ctx)
    uuid = _project_uuid(ctx)
    state_ids = ctx.settings.state_ids
    where = f'rtc_cm:type="{TYPE_ID}"'
    if state_ids:
        clauses = " or ".join(f'rtc_cm:state="{s}"' for s in state_ids)
        where = f"{where} and ({clauses})"

    session = ctx.client.session(server, kind="ewm")
    rows = collect_results(
        session, f"{server}/oslc/contexts/{uuid}/workitems",
        {"oslc.properties": OSLC_PROPERTIES, "oslc.paging": "true",
         "oslc.pageSize": "200", "oslc.where": where},
        timeout=ctx.settings.timeout, verify=ctx.settings.verify, limit=limit)

    items: list[WorkItem] = []
    for row in rows:
        work_item_id = str(row.get("dcterms:identifier") or "").strip()
        if not work_item_id:
            continue
        users, rejected = users_from_workitem(row, work_item_id)
        if rejected:
            log.warning("unparsed_new_users_rows", work_item=work_item_id,
                        count=len(rejected))
        items.append(WorkItem(
            work_item_id=work_item_id,
            summary=(row.get("dcterms:title") or "").strip(),
            state=tail(resource_uri(row.get("rtc_cm:state")), "."),
            access_type=tail(resource_uri(row.get(F_ACCESS_TYPE)), "."),
            domain=tail(resource_uri(row.get(F_DOMAIN)), "."),
            work_areas=tail(resource_uri(row.get(F_WORKAREAS)), "."),
            justification=strip_html(row.get(F_JUSTIFICATION) or ""),
            new_users_raw=(row.get(F_NEW_USERS) or row.get(F_NEW_USERS_LEGACY) or "").strip(),
            users=users,
        ))
    return items


async def fetch_open_requests(ctx: ToolContext, limit: int | None = None) -> list[WorkItem]:
    """Every ALM Access Request in the configured active states."""
    return await to_thread(_fetch_queue, ctx, limit)


def _fetch_one(ctx: ToolContext, work_item_id: str) -> WorkItem | None:
    server = _server(ctx)
    uuid = _project_uuid(ctx)
    session = ctx.client.session(server, kind="ewm")
    rows = collect_results(
        session, f"{server}/oslc/contexts/{uuid}/workitems",
        {"oslc.properties": OSLC_PROPERTIES,
         "oslc.where": f"dcterms:identifier={work_item_id}"},
        timeout=ctx.settings.timeout, verify=ctx.settings.verify, limit=1)
    if not rows:
        return None
    row = rows[0]
    users, _rejected = users_from_workitem(row, work_item_id)
    return WorkItem(work_item_id=work_item_id,
                    summary=(row.get("dcterms:title") or "").strip(),
                    state=tail(resource_uri(row.get("rtc_cm:state")), "."),
                    new_users_raw=(row.get(F_NEW_USERS)
                                   or row.get(F_NEW_USERS_LEGACY) or "").strip(),
                    users=users)


async def fetch_work_item(ctx: ToolContext, work_item_id: str) -> WorkItem | None:
    """One work item by its numeric identifier - the webhook entry point."""
    return await to_thread(_fetch_one, ctx, work_item_id)


# -------------------------------------------------------------------- comments

def _comment_urls(server: str, work_item_id: str) -> list[str]:
    collection = f"{server}/oslc/workitems/{work_item_id}/rtc_cm:comments"
    return [f"{collection}/oslc:comment"]


def _descriptions(node, found: list[str]) -> list[str]:
    """Every dcterms:description string anywhere in an OSLC payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("description") and isinstance(value, str):
                found.append(value)
            else:
                _descriptions(value, found)
    elif isinstance(node, list):
        for item in node:
            _descriptions(item, found)
    return found


def _existing_comments(ctx: ToolContext, work_item_id: str) -> list[str] | None:
    """Comment texts already on the work item, or None when unreadable."""
    server = _server(ctx)
    try:
        response = ctx.client.request(
            "GET", f"{server}/oslc/workitems/{work_item_id}/rtc_cm:comments",
            server=server, kind="ewm", headers=OSLC_HEADERS_JSON)
    except TransportError:
        return None
    if response.status_code != 200:
        return None
    try:
        return _descriptions(response.json(), [])
    except ValueError:
        return None


def _post_comment(ctx: ToolContext, work_item_id: str, text: str,
                  marker: str) -> tuple[bool, str, dict]:
    server = _server(ctx)

    existing = _existing_comments(ctx, work_item_id)
    if existing is not None and any(marker in " ".join(str(c).split()) for c in existing):
        return True, f"identical comment already present {marker}", {"replayed": True}
    if existing is None:
        log.warning("comment_idempotency_unverified", work_item=work_item_id)

    body = json.dumps({
        "dcterms:description": html.escape(text).replace("\n", "<br/>")}).encode("utf-8")
    headers = {"Content-Type": "application/json", **OSLC_HEADERS_JSON, **_csrf(ctx)}

    attempts = []
    for url in _comment_urls(server, work_item_id):
        response = ctx.client.request("POST", url, server=server, kind="ewm",
                                      data=body, headers=headers)
        if response.status_code in (200, 201):
            return True, f"HTTP {response.status_code}", \
                {"location": response.headers.get("Location", "")}
        attempts.append(f"{url}: HTTP {response.status_code} {response.text[:160]}")
    return False, " | ".join(attempts), {}


async def post_comment(ctx: ToolContext, *, work_item_id: str, userid: str, text: str,
                       marker: str) -> ProvisionResult:
    """Comment on a work item, once. The marker makes a re-run a no-op."""
    return await guarded_write(
        ctx, userid=userid, work_item_id=work_item_id,
        operation=Operation.WORKITEM_COMMENT, step="closure",
        action=lambda: to_thread(_post_comment, ctx, work_item_id, text, marker))


# ----------------------------------------------------------------- attachments

def _attachment_titles(ctx: ToolContext, work_item_id: str) -> list[str] | None:
    """Filenames already attached, or None when they cannot be read.

    Compared by name, not URL: a re-upload always mints a new URL, so the CLI's
    URL comparison could never detect a duplicate.
    """
    server = _server(ctx)
    try:
        response = ctx.client.request(
            "GET", f"{server}/oslc/workitems/{work_item_id}", server=server, kind="ewm",
            params={"oslc.properties": ATTACH_PROP}, headers=OSLC_HEADERS_JSON)
        if response.status_code != 200:
            return None
        current = response.json().get(ATTACH_PROP, [])
    except (TransportError, ValueError):
        return None
    current = current if isinstance(current, list) else [current]

    titles: list[str] = []
    for item in current[:100]:
        url = item.get("rdf:resource") if isinstance(item, dict) else None
        if not url:
            continue
        try:
            detail = ctx.client.request("GET", url, server=server, kind="ewm",
                                        headers=OSLC_HEADERS_JSON)
            if detail.status_code == 200:
                data = detail.json()
                title = data.get("dcterms:title") or data.get("oslc:shortTitle") or ""
                if title:
                    titles.append(str(title))
        except (TransportError, ValueError):
            continue
    return titles


def _upload_and_link(ctx: ToolContext, work_item_id: str, path: str,
                     filename: str) -> tuple[bool, str, dict]:
    server = _server(ctx)

    titles = _attachment_titles(ctx, work_item_id)
    if titles is not None and any((t or "").strip().lower() == filename.lower()
                                  for t in titles):
        return True, f"{filename} already attached", {"replayed": True}
    if titles is None:
        log.warning("attachment_idempotency_unverified", work_item=work_item_id)

    uuid = _project_uuid(ctx)
    with open(path, "rb") as handle:
        payload = handle.read()
    response = ctx.client.request(
        "POST", f"{server}{UPLOAD_PATH}?projectId={uuid}&multiple=true",
        server=server, kind="ewm",
        files={"attach": (filename, payload, "image/png")},
        headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest", **_csrf(ctx)})
    if response.status_code != 200:
        return False, f"upload failed: HTTP {response.status_code}", \
            {"body": response.text[:200]}

    # The JSON is wrapped in <html><body><textarea>...</textarea></body></html>.
    body = response.text
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < 0:
        raise DataError("unexpected attachment upload response",
                        context={"body": body[:200]})
    files = json.loads(body[start:end + 1]).get("files", [])
    if not files or "url" not in files[0]:
        raise DataError("attachment upload response carried no file URL")
    attachment_url = files[0]["url"]

    work_item_url = f"{server}/oslc/workitems/{work_item_id}"
    headers = {"Content-Type": "application/json", **OSLC_HEADERS_JSON, **_csrf(ctx)}
    current = ctx.client.request("GET", work_item_url, server=server, kind="ewm",
                                 params={"oslc.properties": ATTACH_PROP}, headers=headers)
    if current.status_code != 200:
        return False, f"work item read failed: HTTP {current.status_code}", {}
    links = current.json().get(ATTACH_PROP, [])
    links = links if isinstance(links, list) else [links]
    links.append({"rdf:resource": attachment_url})

    linked = ctx.client.request(
        "PUT", f"{work_item_url}?oslc.properties={ATTACH_PROP}", server=server, kind="ewm",
        data=json.dumps({ATTACH_PROP: links}),
        headers={**headers, "If-Match": current.headers.get("ETag", "*")})
    if linked.status_code in (200, 204):
        return True, f"attached {filename}", {"attachment_url": attachment_url}
    return False, f"link failed: HTTP {linked.status_code}", {"body": linked.text[:200]}


async def attach_evidence(ctx: ToolContext, *, work_item_id: str, userid: str,
                          path: str, filename: str) -> ProvisionResult:
    """Upload one evidence file and link it to the work item, once."""
    return await guarded_write(
        ctx, userid=userid, work_item_id=work_item_id,
        operation=Operation.WORKITEM_ATTACH, step="evidence",
        action=lambda: to_thread(_upload_and_link, ctx, work_item_id, path, filename))
