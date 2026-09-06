"""JTS registry tools: look a user up in LDAP, import them, reactivate, verify.

The endpoints are the ones the JTS admin UI itself calls, established by
inspecting that UI. They are internal services with no published contract, which
is why every response is validated here rather than trusted:

  searchRegistry            LDAP lookup - authoritative name, e-mail, validity
  multipleNewContributors   creates the repository contributor from the LDAP entry
  contributorByUserId       roles list plus the archived flag
  /users/<id> (RDF)         read-modify-write to clear jfs:archived

The registry is LDAP-backed and read-only, so a contributor is *imported*, never
created from scratch; a plain foaf:Person POST is rejected by the server.
"""
from __future__ import annotations

import json
import re

from ..errors import DataError, NotFoundError, TransportError
from ..logging import get_logger
from ..models import Operation, ProvisionResult, RequestedUser, RiskLevel, UserState, UserStatus
from .base import ToolContext, guarded_write, to_thread

log = get_logger("alm.tools.jts")

SEARCH_PATH = ("/service/com.ibm.team.repository.service.internal."
               "IExternalUserRegistryRestService/searchRegistry")
CREATE_PATH = ("/service/com.ibm.team.repository.service.internal."
               "IAdminRestService/multipleNewContributors")
DETAILS_PATH = ("/service/com.ibm.team.repository.service.internal."
                "IAdminRestService/contributorByUserId")

# Headers the JTS web client sends on these service calls; without them the
# services answer with an HTML error page.
XHR_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "X-com-ibm-team-configuration-versions": "LATEST",
    "Accept": "text/json",
}
RDF = "application/rdf+xml"
_ARCHIVED_RE = re.compile(r"(<jfs:archived\b[^>]*>)\s*(true|false)\s*(</jfs:archived>)",
                          re.I | re.S)


def _server(ctx: ToolContext) -> str:
    return ctx.settings.jts_server


def _envelope(payload: dict) -> dict:
    """Unwrap the SOAP/JSON envelope these internal services return."""
    try:
        return payload["soapenv:Body"]["response"]["returnValue"]["value"]
    except (KeyError, TypeError) as err:
        raise DataError("unexpected JTS service envelope",
                        context={"keys": list(payload)[:8]}) from err


# ------------------------------------------------------------------ read paths

def _search_registry(ctx: ToolContext, userid: str, hide_existing: bool) -> list[dict]:
    server = _server(ctx)
    response = ctx.client.request(
        "POST", f"{server}{SEARCH_PATH}", server=server, kind="jts",
        data={"searchText": userid, "hideExistingUsers": "true" if hide_existing else "false"},
        headers=XHR_HEADERS)
    if response.status_code != 200:
        raise TransportError(f"searchRegistry returned HTTP {response.status_code}",
                             context={"userid": userid})
    try:
        value = _envelope(response.json())
    except ValueError as err:
        raise DataError("searchRegistry did not return JSON") from err

    matches = []
    for entry in value.get("externalUsers", []) or []:
        # User IDs are case sensitive in JTS; an exact match is the only match.
        if userid in (entry.get("userIds") or []):
            matches.append({
                "userId": userid,
                "name": (entry.get("fullNames") or [userid])[0],
                "email": (entry.get("emailAddresses") or [""])[0],
                "valid": bool((entry.get("status") or {}).get("valid", False)),
            })
    return matches


def _contributor_rdf(ctx: ToolContext, userid: str) -> tuple[str | None, str | None, bool | None]:
    server = _server(ctx)
    response = ctx.client.request("GET", f"{server}/users/{userid}", server=server,
                                  kind="jts", headers={"Accept": RDF},
                                  allow_redirects=True)
    content_type = response.headers.get("Content-Type", "").lower()
    if response.status_code != 200 or "html" in content_type or "rdf" not in content_type:
        return None, None, None
    match = _ARCHIVED_RE.search(response.text)
    archived = (match.group(2).lower() == "true") if match else None
    return response.headers.get("ETag"), response.text, archived


def _contributor_details(ctx: ToolContext, userid: str) -> dict | None:
    server = _server(ctx)
    response = ctx.client.request("GET", f"{server}{DETAILS_PATH}", server=server,
                                  kind="jts", params={"userId": userid},
                                  headers=XHR_HEADERS)
    if response.status_code != 200:
        return None
    try:
        value = _envelope(response.json())
    except (DataError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("userId") == userid else None


def _classify(ctx: ToolContext, user: RequestedUser, role: str) -> UserStatus:
    """Decide what state a user is in, and how risky provisioning them is."""
    userid = user.userid
    ready = _search_registry(ctx, userid, hide_existing=True)
    details = _contributor_details(ctx, userid)
    has_role = bool(details and role in (details.get("roles") or [])
                    and not details.get("archived", False))

    if ready:
        info = ready[0]
        state = UserState.READY if info["valid"] else UserState.INVALID
        ldap_name, ldap_email, valid = info["name"], info["email"], info["valid"]
    else:
        existing = _search_registry(ctx, userid, hide_existing=False)
        if existing:
            info = existing[0]
            ldap_name, ldap_email, valid = info["name"], info["email"], info["valid"]
            _etag, rdf, archived = _contributor_rdf(ctx, userid)
            if rdf is None:
                state = UserState.EXISTS  # present in the registry, RDF unreadable
            else:
                state = UserState.ARCHIVED if archived else UserState.EXISTS
        else:
            state = UserState.MISSING
            ldap_name = ldap_email = ""
            valid = False

    risk, reasons = _risk(user, state, ldap_email)
    return UserStatus(userid=userid, state=state, ldap_name=ldap_name,
                      ldap_email=ldap_email, valid_in_ldap=valid, has_role=has_role,
                      role=role, risk=risk, risk_reasons=reasons)


def _risk(user: RequestedUser, state: UserState, ldap_email: str) -> tuple[RiskLevel, list[str]]:
    """Flag the things a human approver should actually look at."""
    reasons: list[str] = []
    if user.extracted_by_llm:
        reasons.append("user ID was recovered by the LLM fallback, not the structured field")
    if state in (UserState.MISSING, UserState.INVALID):
        reasons.append(f"LDAP state is {state.value} - provisioning cannot succeed")
    if state == UserState.ARCHIVED:
        reasons.append("account exists but is archived; it will be reactivated")
    if user.email and ldap_email and user.email.lower() != ldap_email.lower():
        reasons.append(f"requested e-mail {user.email} differs from LDAP {ldap_email}")
    if len(user.source_work_items) > 1:
        reasons.append(f"requested on {len(user.source_work_items)} work items")

    if any(r.startswith("user ID was recovered") or "cannot succeed" in r for r in reasons):
        return RiskLevel.HIGH, reasons
    if reasons:
        return RiskLevel.MEDIUM, reasons
    return RiskLevel.LOW, reasons


async def classify_user(ctx: ToolContext, user: RequestedUser) -> UserStatus:
    """Validation agent entry point: what is true about this user right now."""
    return await to_thread(_classify, ctx, user, ctx.settings.jazz_role)


async def check_role(ctx: ToolContext, userid: str, role: str = "") -> bool:
    """True when the user holds the repository role and is not archived."""
    role = role or ctx.settings.jazz_role
    details = await to_thread(_contributor_details, ctx, userid)
    if not details:
        return False
    return role in (details.get("roles") or []) and not details.get("archived", False)


# ----------------------------------------------------------------- write paths

def _create_contributor(ctx: ToolContext, info: dict) -> tuple[bool, str, dict]:
    server = _server(ctx)
    payload = [{"name": info["name"], "userId": info["userId"],
                "emailAddress": info["email"]}]
    response = ctx.client.request(
        "POST", f"{server}{CREATE_PATH}", server=server, kind="jts",
        data={"jsonUserInfo": json.dumps(payload)}, headers=XHR_HEADERS)
    if response.status_code not in (200, 201):
        return False, f"HTTP {response.status_code}", {"body": response.text[:300]}
    body = response.text
    # Success is a SOAP/JSON response envelope; a fault or a stack trace is not.
    if "soapenv:Fault" in body or "stackTrace" in body or '"response"' not in body:
        return False, "JTS returned a server fault", {"body": body[:300]}

    # Independent confirmation: re-read the contributor rather than believing
    # the write's own response.
    _etag, rdf, archived = _contributor_rdf(ctx, info["userId"])
    if rdf is None:
        return False, "contributor not readable after the write", {}
    if archived:
        return False, "contributor exists but is archived after the write", {}
    return True, "created and confirmed active", {"email": info["email"]}


def _unarchive(ctx: ToolContext, userid: str) -> tuple[bool, str, dict]:
    server = _server(ctx)
    etag, rdf, archived = _contributor_rdf(ctx, userid)
    if rdf is None:
        raise NotFoundError(f"contributor RDF unreadable for {userid}")
    if archived is None:
        return False, "contributor has no jfs:archived property; refusing to guess", {}
    if not archived:
        return True, "already active", {}

    new_rdf = _ARCHIVED_RE.sub(lambda m: m.group(1) + "false" + m.group(3), rdf, count=1)
    headers = {"Content-Type": RDF, "Accept": RDF, "X-Requested-With": "XMLHttpRequest"}
    if etag:
        # JFS 7.0.2 SR1 wants the concurrency token in a header named ETag and
        # rejects the standard If-Match with CRJZS5488E. Send both.
        headers["ETag"] = etag
        headers["If-Match"] = etag
    response = ctx.client.request("PUT", f"{server}/users/{userid}", server=server,
                                  kind="jts", data=new_rdf.encode("utf-8"),
                                  headers=headers)
    if response.status_code not in (200, 204):
        return False, f"unarchive PUT returned HTTP {response.status_code}", \
            {"body": response.text[:300]}

    _etag, _rdf, now_archived = _contributor_rdf(ctx, userid)
    if now_archived is not False:
        return False, f"PUT succeeded but the re-read still shows archived={now_archived}", {}
    return True, "reactivated and confirmed active", {}


async def provision_user(ctx: ToolContext, user: RequestedUser,
                         status: UserStatus) -> ProvisionResult:
    """Import or reactivate one user, guarded by approval and idempotency.

    A user who is already active is a no-op that still produces an audit row -
    that record is what lets the closure agent say "already present" instead of
    claiming an import that never happened.
    """
    work_item_id = user.work_item_ids[0] if user.work_item_ids else ""

    if status.state == UserState.EXISTS:
        from ..models import Outcome

        result = ProvisionResult(
            userid=user.userid, operation=Operation.JTS_CREATE, outcome=Outcome.SKIPPED,
            work_item_id=work_item_id, message="already an active JTS contributor")
        from .base import record

        await record(ctx, result, "jts_provision")
        return result

    if status.blocked:
        from ..models import Outcome

        result = ProvisionResult(
            userid=user.userid, operation=Operation.JTS_CREATE, outcome=Outcome.FAILED,
            work_item_id=work_item_id,
            message=f"cannot provision: LDAP state is {status.state.value}")
        from .base import record

        await record(ctx, result, "jts_provision")
        return result

    if status.state == UserState.ARCHIVED:
        return await guarded_write(
            ctx, userid=user.userid, work_item_id=work_item_id,
            operation=Operation.JTS_UNARCHIVE, step="jts_provision",
            action=lambda: to_thread(_unarchive, ctx, user.userid))

    info = {"userId": user.userid,
            "name": status.ldap_name or user.display_name,
            "email": status.ldap_email or (user.email or "")}
    return await guarded_write(
        ctx, userid=user.userid, work_item_id=work_item_id,
        operation=Operation.JTS_CREATE, step="jts_provision",
        action=lambda: to_thread(_create_contributor, ctx, info))
