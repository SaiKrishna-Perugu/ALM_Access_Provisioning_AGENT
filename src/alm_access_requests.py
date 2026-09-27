"""
Fetch ALM Access Request work items from the "Unified Tracking System (Change
Management)" project area on Chrysler EWM and print the requested fields.

The saved query "ALM Access Request Pending ICT Action - Internal ALM" maps to:
  Type = ALM Access Request (internal type, not External/Supplier)  AND
  State = "In Progress - ICT"  (i.e. pending ICT action)

Use --state to target a different workflow state, or --all-open for the active
queue (Submitted, Approved, In Progress, In Progress - ICT).

Use --project-uuid or EWM_PROJECT_UUID to avoid project discovery, and
--state-id or EWM_WORKFLOW_STATE_ID / EWM_ACTIVE_STATE_IDS to avoid workflow
state resolution.

No Excel output - results are printed (and optionally written to CSV with --csv).
Run on the Chrysler intranet / VPN.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import html
import json
import os
import re
import sys

import defusedxml.ElementTree as ET
import requests
from defusedxml.common import DefusedXmlException

import alm_config
import alm_log
import jazz_client


def _load_local_env() -> None:
    """Load a nearby .env file without requiring python-dotenv."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ[key] = value

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv optional
    _load_local_env()

# Some EWM/OSLC servers (notably TEST) return XML containing invalid control
# characters or unescaped ampersands that break the strict ElementTree parser.
_XML_INVALID_CHARS = re.compile(
    "[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
_XML_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)")


def parse_xml(content):
    """Parse XML defensively; sanitize invalid chars / stray ampersands on failure."""
    text = content.decode("utf-8", "replace") if isinstance(content, bytes | bytearray) else content
    head = text.lstrip()[:64].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        raise RuntimeError(
            "Server returned an HTML page (the Jazz web UI), not OSLC XML. The session "
            "is not authorized for API access on this server - verify EWM_SERVER and that "
            "your account has access there (this endpoint works on the PROD ccm server)."
        )
    try:
        return ET.fromstring(text)
    except (ET.ParseError, DefusedXmlException) as err:
        if isinstance(err, DefusedXmlException):
            raise RuntimeError(f"Hostile XML payload rejected: {err}") from err
        cleaned = _XML_BARE_AMP.sub("&amp;", _XML_INVALID_CHARS.sub("", text))
        try:
            return ET.fromstring(cleaned)
        except (ET.ParseError, DefusedXmlException) as err2:
            raise RuntimeError(f"Unparseable OSLC XML: {err2}") from err2


SERVER = alm_config.env_or("EWM_SERVER", "https://ewm.example.intra/ccm")
PROJECT_NAME = "Unified Tracking System (Change Management)"
TYPE_ID = "com.fca.alm.rtc.workitem.workItemType.almAccessRequest"
WORKFLOW = "com.ibm.team.workitem.almAccessRequestWorkflow"
PROJECT_UUID = os.getenv("EWM_PROJECT_UUID", "").strip()
STATE_ID = os.getenv("EWM_WORKFLOW_STATE_ID", "").strip()
ACTIVE_STATE_IDS = os.getenv("EWM_ACTIVE_STATE_IDS", "").strip()

# Where the parsed user IDs are stored so the JTS import agent can pick them up.
USERS_OUT_DEFAULT = os.getenv("ALM_USERS_OUT", "out/alm_users.json")

# custom attribute ids (rtc_ext namespace)
A = "rtc_ext:com.fca.alm.rtc.almAccessRequestOverview."
F_ACCESS_TYPE = A + "chnType"
F_NEW_USERS = A + "newUs"            # legacy FCA field (often empty)
F_NEW_USERS_STLA = "rtc_ext:com.stellantis.alm.rtc.aar.newUsers"        # current Stellantis field
F_EXISTING = A + "existingUsers"
F_EXISTING_STLA = "rtc_ext:com.stellantis.alm.rtc.aar.existingUsers"
F_DOMAIN = A + "domain"
F_WORKAREAS = A + "workAreas"
F_JUSTIFICATION = A + "justification"

FIELDS = [
    "Type", "ID", "Summary", "Access Type", "Status", "New Users", "Existing User(s)",
    "Domain", "Work Area(s)", "Roles", "Approvals", "Planned For",
    "Created By", "Creation Date", "Modified By", "Modified Date",
]

OSLC_PROPERTIES = ",".join([
    "dcterms:identifier",
    "dcterms:title",
    "dcterms:type",
    "rtc_cm:state",
    "oslc_cm:status",
    "dcterms:creator",
    "rtc_cm:modifiedBy",
    "dcterms:created",
    "dcterms:modified",
    F_ACCESS_TYPE,
    F_NEW_USERS,
    F_NEW_USERS_STLA,
    F_EXISTING,
    F_EXISTING_STLA,
    F_DOMAIN,
    F_WORKAREAS,
    F_JUSTIFICATION,
    "rtc_cm:plannedFor",
    "oslc_cm:approved",
])


def ln(t: str) -> str:
    return t.rsplit("}", 1)[-1]


def strip_html(text: str) -> str:
    """Turn the rich-text justification into readable single-line plain text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "; ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip(" ;")


def _session_reads_api(s: requests.Session) -> bool:
    """True only if a protected OSLC resource returns XML (not the Jazz web UI).

    This is EWM's own proof that the session is live, and it is intentionally
    unlike the JTS check (/whoami) and the browser check (a user ID in an input
    value). Verification that shares a mechanism with the thing it verifies is
    how the login page ended up attached to 11 production work items.
    """
    r = s.get(f"{SERVER}/process/project-areas", headers={"Accept": "application/xml"},
              timeout=jazz_client.DEFAULT_TIMEOUT)
    ct = r.headers.get("Content-Type", "").lower()
    return "html" not in ct and r.text.lstrip()[:5].lower().startswith("<?xml")


def login(user: str, password: str) -> requests.Session:
    """Authenticate to EWM. The endpoint discovery and TLS policy live in jazz_client."""
    s = jazz_client.make_session()
    if jazz_client.form_login(s, SERVER, user, password, _session_reads_api):
        alm_log.say("[OK] Authenticated with EWM.", "ewm_login_ok", server=SERVER)
        return s

    raise RuntimeError(
        f"Authentication failed on {SERVER} - check the password/account (the same CID is "
        "used for TEST and PROD, but you must be on the intranet / VPN)."
    )


def _uuid_valid_here(s: requests.Session, uuid: str) -> bool:
    """True if the cached UUID names a project area that exists on the current SERVER."""
    try:
        r = s.get(f"{SERVER}/process/project-areas/{uuid}",
                  headers={"Accept": "application/xml"}, timeout=(15, 60))
    except requests.RequestException:
        return False
    return r.status_code == 200


def project_uuid(s: requests.Session, explicit_uuid: str | None = None, project_name: str = PROJECT_NAME) -> str:
    if explicit_uuid:
        return explicit_uuid
    # A cached UUID is only valid on the server it came from, so verify it before
    # trusting it - otherwise a TEST/PROD switch silently queries the wrong project.
    if PROJECT_UUID and _uuid_valid_here(s, PROJECT_UUID):
        return PROJECT_UUID
    if PROJECT_UUID:
        print(f"[warn] EWM_PROJECT_UUID not valid on {SERVER}; rediscovering project area.")

    r = s.get(f"{SERVER}/process/project-areas", headers={"Accept": "application/xml"})
    root = parse_xml(r.content)
    for pa in root.iter():
        if ln(pa.tag) != "project-area":
            continue
        name = next((v for k, v in pa.attrib.items() if ln(k) == "name"), "")
        if name.strip() == project_name:
            for c in pa:
                if ln(c.tag) == "url" and c.text:
                    return c.text.rstrip("/").split("/")[-1]
    raise RuntimeError(f"Project area not found: {project_name}")


def workflow_states(s: requests.Session, uuid: str):
    """Return (sid->name, name->state-identifier-string)."""
    url = f"{SERVER}/oslc/workflows/{uuid}/states/{WORKFLOW}"
    r = s.get(url, headers={"Accept": "application/xml", "OSLC-Core-Version": "2.0"}, timeout=(15, 60))
    sid_name, name_ident = {}, {}
    for el in parse_xml(r.content).iter():
        if ln(el.tag) not in ("State", "Status"):
            continue
        about = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")
        title = next((c.text.strip() for c in el if ln(c.tag) in ("title", "name") and c.text), "")
        if not about or not title:
            continue
        ident = about.rsplit("/", 1)[-1]          # e.g. ...almAccessRequestWorkflow.state.s4
        sid_name[ident.rsplit(".", 1)[-1]] = title  # s4 -> "In Progress - ICT"
        name_ident[title.lower()] = ident
    return sid_name, name_ident


def resolve_state_ids(s: requests.Session, uuid: str, all_open: bool, state_name: str | None, state_id: str | None) -> list[str]:
    if all_open:
        if ACTIVE_STATE_IDS:
            return [x.strip() for x in ACTIVE_STATE_IDS.split(",") if x.strip()]
        _, name_ident = workflow_states(s, uuid)
        wanted = ["submitted", "approved", "in progress", "in progress - ict"]
        result = [name_ident[n] for n in wanted if n in name_ident]
        if not result:
            raise RuntimeError("Unable to resolve active queue state identifiers.")
        return result

    if state_id or STATE_ID:
        return [state_id or STATE_ID]
    if not state_name:
        raise RuntimeError("A workflow state name or state identifier is required.")
    _, name_ident = workflow_states(s, uuid)
    ident = name_ident.get(state_name.lower())
    if not ident:
        raise RuntimeError(f"Unknown state '{state_name}'. Available: {', '.join(sorted(name_ident.keys()))}")
    return [ident]


class Resolver:
    """Caches enumeration-literal and iteration lookups.

    By default the resolver avoids any network calls and returns local tokens.
    Set `resolve=True` to allow additional OSLC requests to fetch human-readable labels.
    """

    def __init__(self, s: requests.Session, resolve: bool = False):
        self.s = s
        self.resolve = bool(resolve)
        self.enum: dict[str, dict[str, str]] = {}
        self.iter: dict[str, str] = {}

    def enum_label(self, uri: str) -> str:
        # When not resolving, return the last token portion to avoid extra HTTP calls.
        if not self.resolve or not uri:
            return uri.rsplit(".", 1)[-1] if uri else ""
        parent = uri.rsplit("/", 1)[0]
        if parent not in self.enum:
            self.enum[parent] = {}
            try:
                r = self.s.get(parent, headers={"Accept": "application/xml", "OSLC-Core-Version": "2.0"}, timeout=(15, 60))
                for el in parse_xml(r.content).iter():
                    about = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")
                    if ".literal." not in about:
                        continue
                    title = next((c.text.strip() for c in el if ln(c.tag) in ("title", "name") and c.text), "")
                    if title:
                        self.enum[parent][about] = title
            except (requests.RequestException, ET.ParseError, ValueError) as err:
                # Labels are cosmetic - fall back to the raw token, but say so
                # rather than silently degrading the output.
                alm_log.warn(f"[warn] could not resolve enumeration labels from {parent}: "
                             f"{type(err).__name__}: {err}", "enum_resolve_failed", uri=parent)
        return self.enum[parent].get(uri, uri.rsplit(".", 1)[-1])

    def iteration(self, uri: str) -> str:
        if not uri:
            return ""
        if not self.resolve:
            return uri.rsplit("/", 1)[-1]
        if uri not in self.iter:
            self.iter[uri] = uri.rsplit("/", 1)[-1]
            try:
                r = self.s.get(uri, headers={"Accept": "application/json", "OSLC-Core-Version": "2.0"}, timeout=(15, 60))
                if r.status_code == 200:
                    self.iter[uri] = r.json().get("dcterms:title", self.iter[uri])
            except (requests.RequestException, ValueError) as err:
                alm_log.warn(f"[warn] could not resolve iteration title for {uri}: "
                             f"{type(err).__name__}: {err}", "iteration_resolve_failed", uri=uri)
        return self.iter[uri]


def res_uri(v):
    return v.get("rdf:resource") if isinstance(v, dict) else None


def user_id(v) -> str:
    u = res_uri(v)
    return u.rsplit("/", 1)[-1] if u else ""


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def extract(item: dict, states: dict, rv: Resolver) -> dict:
    access = ", ".join(rv.enum_label(u) for u in (res_uri(x) for x in as_list(item.get(F_ACCESS_TYPE))) if u)
    existing = ", ".join(user_id(x) for x in as_list(item.get(F_EXISTING)))
    existing_stla = (item.get(F_EXISTING_STLA) or "").strip()
    if existing_stla:
        existing = existing_stla if not existing else f"{existing_stla}; {existing}"
    workareas = ", ".join(rv.enum_label(u) for u in (res_uri(x) for x in as_list(item.get(F_WORKAREAS))) if u)
    domain = item.get(F_DOMAIN)
    domain_lbl = rv.enum_label(res_uri(domain)) if res_uri(domain) else ""
    state_uri = res_uri(item.get("rtc_cm:state")) or ""
    status = states.get(state_uri.rsplit(".", 1)[-1], state_uri.rsplit("/", 1)[-1]) if state_uri else item.get("oslc_cm:status", "")
    planned = item.get("rtc_cm:plannedFor")
    planned_lbl = rv.iteration(res_uri(planned)) if res_uri(planned) else ""
    approved = item.get("oslc_cm:approved")
    new_users = (item.get(F_NEW_USERS_STLA) or item.get(F_NEW_USERS) or "").strip()
    if not new_users:
        parts = []
        summary = (item.get("dcterms:title") or "").strip()
        if summary:
            parts.append(f"(from Summary) {summary}")
        just = strip_html(item.get(F_JUSTIFICATION) or "")
        if just:
            parts.append(f"(from Justification) {just}")
        new_users = " | ".join(parts)
    return {
        "Type": item.get("dcterms:type", ""),
        "ID": item.get("dcterms:identifier", ""),
        "Summary": item.get("dcterms:title", ""),
        "Access Type": access,
        "Status": status,
        "New Users": new_users,
        "Existing User(s)": existing,
        "Domain": domain_lbl,
        "Work Area(s)": workareas,
        "Roles": "",  # no Roles attribute defined on this work item type
        "Approvals": "Approved" if approved else ("Not approved" if approved is not None else ""),
        "Planned For": planned_lbl,
        "Created By": user_id(item.get("dcterms:creator")),
        "Creation Date": item.get("dcterms:created", ""),
        "Modified By": user_id(item.get("rtc_cm:modifiedBy")),
        "Modified Date": item.get("dcterms:modified", ""),
    }


def parse_new_users(new_users: str) -> list[dict]:
    """Parse the 'LASTNAME,FIRSTNAME,email,USERID;' New Users field into records.

    Free-text fallbacks (e.g. '(from Summary) ...') yield no records because they
    contain no structured USERID token.
    """
    users: list[dict] = []
    if not new_users or new_users.lstrip().startswith("(from "):
        return users
    for entry in new_users.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(",")]
        # Expected: LAST, FIRST, email, USERID (email is second-to-last, id is last).
        if len(parts) < 4 or "@" not in parts[-2] or not parts[-1]:
            continue
        users.append({
            "userid": parts[-1],
            "email": parts[-2],
            "first_name": ",".join(parts[1:-2]).strip(),
            "last_name": parts[0],
        })
    return users


def collect_users(rows: list[dict]) -> list[dict]:
    """Aggregate unique users (by USERID) across work items, keeping their sources."""
    by_id: dict[str, dict] = {}
    for row in rows:
        for u in parse_new_users(row.get("New Users", "")):
            src = {
                "work_item_id": row.get("ID", ""),
                "summary": row.get("Summary", ""),
                "access_type": row.get("Access Type", ""),
                "domain": row.get("Domain", ""),
                "work_areas": row.get("Work Area(s)", ""),
            }
            rec = by_id.get(u["userid"])
            if rec is None:
                rec = dict(u)
                rec["source_work_items"] = [src]
                by_id[u["userid"]] = rec
            else:
                rec["source_work_items"].append(src)
    return list(by_id.values())


def write_users_file(users: list[dict], path: str) -> None:
    """Persist the parsed users so the JTS import agent can read them back."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "source": "alm_access_requests.py",
        "count": len(users),
        "users": users,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def fetch(s: requests.Session, uuid: str, where: str, limit: int | None):
    url = f"{SERVER}/oslc/contexts/{uuid}/workitems"
    H = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}
    params = {"oslc.properties": OSLC_PROPERTIES, "oslc.paging": "true", "oslc.pageSize": "200", "oslc.where": where}
    items, next_url, page = [], url, 0
    while next_url:
        page += 1
        r = s.get(next_url, headers=H, params=params if page == 1 else None, timeout=(15, 120))
        if r.status_code != 200:
            raise RuntimeError(f"Query failed: HTTP {r.status_code} - {r.text[:300]}")
        data = r.json()
        items.extend(data.get("oslc:results", []))
        if limit and len(items) >= limit:
            return items[:limit]
        info = data.get("oslc:responseInfo", {})
        next_url = data.get("oslc:nextPage") or (info.get("oslc:nextPage") if isinstance(info, dict) else None)
        if isinstance(next_url, dict):
            next_url = next_url.get("rdf:resource")
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch ALM Access Request work items.")
    ap.add_argument("--project", default=None, help="Project area name (default is the configured Unified Tracking System project).")
    ap.add_argument("--project-uuid", default=None, help="Project area UUID; if set, avoids project discovery.")
    ap.add_argument("--user", default=os.getenv("EWM_USER") or os.getenv("CID"),
                    help="EWM username (defaults to CID/EWM_USER from .env; not prompted for)")
    ap.add_argument("--state", default="In Progress - ICT", help='Workflow state (default "In Progress - ICT")')
    ap.add_argument("--state-id", default=None, help="Workflow state identifier, e.g. almAccessRequestWorkflow.state.s4. If set, avoids workflow state discovery.")
    ap.add_argument("--all-open", action="store_true", help="All active states (Submitted, Approved, In Progress, In Progress - ICT)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--csv", default=None, help="Optional CSV output path")
    ap.add_argument("--users-out", default=USERS_OUT_DEFAULT,
                    help=f"Path to write parsed user IDs as JSON for the JTS import agent (default {USERS_OUT_DEFAULT}). Use '' to skip.")
    ap.add_argument("--resolve", action="store_true", help="Resolve enum and iteration URIs with extra OSLC requests (off by default)")
    args = ap.parse_args()
    if not args.user:
        ap.error("No CID configured. Set CID in .env (see .env.example) or pass --user.")
    # The CID is fixed (never prompted for). Only the password is sensitive and is
    # always prompted for, unless the pipeline orchestrator already collected it once
    # and passed it via the EWM_PASSWORD environment variable (never stored on disk).
    password = os.getenv("EWM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    alm_config.print_banner("retrieve (read-only)", commit=False)

    try:
        s = login(args.user, password)
        project = args.project or PROJECT_NAME
        uuid = project_uuid(s, args.project_uuid, project)

        base = f'rtc_cm:type="{TYPE_ID}"'
        if args.all_open:
            state_ids = [x.strip() for x in ACTIVE_STATE_IDS.split(",") if x.strip()] if ACTIVE_STATE_IDS else None
            state_ids = state_ids or resolve_state_ids(s, uuid, True, None, None)
            clauses = [f'rtc_cm:state="{state_id}"' for state_id in state_ids]
            where = f"{base} and ({' or '.join(clauses)})"
            label = "active queue"
        else:
            state_ids = resolve_state_ids(s, uuid, False, args.state, args.state_id)
            where = f'{base} and rtc_cm:state="{state_ids[0]}"'
            label = args.state if args.state_id is None else args.state_id

        print(f"Fetching ALM Access Requests for project '{project}' ({label})...")
        items = fetch(s, uuid, where, args.limit)
        rv = Resolver(s, resolve=args.resolve)
        rows = [extract(it, {}, rv) for it in items]
        print(f"\n{len(rows)} work item(s)\n")

        for row in rows:
            print("=" * 70)
            for f in FIELDS:
                print(f"{f:16}: {row[f]}")
        print("=" * 70)

        if args.csv and rows:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(rows)
            print(f"\n[OK] Wrote {len(rows)} rows to {args.csv}")

        if args.users_out:
            users = collect_users(rows)
            write_users_file(users, args.users_out)
            ids = ", ".join(u["userid"] for u in users)
            print(f"\n[OK] Stored {len(users)} user ID(s) to {args.users_out}")
            if ids:
                print(f"User IDs: {ids}")
    except Exception as err:  # noqa: BLE001
        print(f"\n[ERROR] {err}")
        if isinstance(err, requests.exceptions.ConnectionError | requests.exceptions.Timeout):
            print("Ensure you are on the Chrysler intranet / VPN.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
