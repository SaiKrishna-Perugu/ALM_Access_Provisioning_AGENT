"""
Retrieve work items from a specific IBM EWM / RTC project area via the
OSLC CM REST API (lightweight, no Jazz SDK / JARs required).

Server: Chrysler EWM (EWM_SERVER from .env; PROD prsse by default).
Must run on the corporate network or VPN.

Usage:
    python ewm_workitems.py --project "Unified Tracking System (Change Management)"
    python ewm_workitems.py --project "My Project Area" --csv workitems.csv
    python ewm_workitems.py --project "My Project Area" --where 'dcterms:type="defect"'

Config precedence: CLI args > environment variables > .env file.
Required: EWM_SERVER, CID (or EWM_USER), and a prompted password.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import os
import sys

# defusedxml does the parsing; Element is only the stdlib type it returns.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from xml.etree.ElementTree import Element  # noqa: S405 - type only, never parses

import defusedxml.ElementTree as ET
import requests

import alm_config
import jazz_client

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: S110 - dotenv optional
    pass

DEFAULT_SERVER = alm_config.env_or("EWM_SERVER", "https://ewm.example.intra/ccm")


def get_authenticated_session(server: str, username: str, password: str) -> requests.Session:
    """Log in to the Jazz server using Form authentication (j_security_check).

    Shares jazz_client's endpoint discovery and TLS policy with the other
    modules; keeps its own success signal (the absence of the Jazz auth-challenge
    header on a protected resource).
    """
    auth_url = f"{server}/authenticated/identity"

    def challenge_cleared(sess: requests.Session) -> bool:
        check = sess.get(auth_url, timeout=jazz_client.DEFAULT_TIMEOUT)
        return "X-com.ibm.team.repository.web.auth.request" not in check.headers

    session = jazz_client.make_session()
    if not jazz_client.form_login(session, server, username, password, challenge_cleared):
        raise RuntimeError(
            "Authentication failed. Verify credentials, or the server may use "
            "SSO (PingFederate/Okta/Kerberos) instead of native Jazz Form auth."
        )
    print("[OK] Authenticated with EWM.")
    return session


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(elem: Element, local: str) -> str:
    """Get an attribute by local name, ignoring its XML namespace prefix."""
    for key, val in elem.attrib.items():
        if _localname(key) == local:
            return val
    return ""


def list_project_areas(session: requests.Session, server: str) -> list[tuple[str, str]]:
    """Return [(name, uuid)] for every project area the user can see."""
    resp = session.get(f"{server}/process/project-areas", headers={"Accept": "application/xml"})
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch project areas: HTTP {resp.status_code}")

    root = ET.fromstring(resp.content)
    found: list[tuple[str, str]] = []
    for pa in root.iter():
        if _localname(pa.tag) != "project-area":
            continue
        name = _attr(pa, "name").strip()
        uuid = ""
        for child in pa:
            if _localname(child.tag) == "url" and child.text:
                uuid = child.text.rstrip("/").split("/")[-1]
                break
        if name:
            found.append((name, uuid))
    return found


def get_project_area_uuid(session: requests.Session, server: str, target_name: str) -> str:
    """Resolve a project area display name to its UUID (case-insensitive / partial)."""
    areas = list_project_areas(session, server)
    target = target_name.strip().lower()
    # exact match first, then a forgiving 'contains' match
    for name, uuid in areas:
        if name.strip().lower() == target and uuid:
            return uuid
    for name, uuid in areas:
        if target in name.strip().lower() and uuid:
            print(f"[i] Matched '{name}' for query '{target_name}'.")
            return uuid
    sample = ", ".join(n for n, _ in areas[:10]) or "(none returned)"
    raise RuntimeError(
        f"Project area '{target_name}' not found. Visible areas include: {sample}. "
        "Run with --list to see all names."
    )


def fetch_work_items(
    session: requests.Session,
    server: str,
    project_uuid: str,
    properties: str,
    where: str | None,
    page_size: int,
    limit: int | None,
    timeout: int,
) -> list[dict]:
    """Query OSLC for work items in the project context, following pagination."""
    url = f"{server}/oslc/contexts/{project_uuid}/workitems"
    headers = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}
    params = {
        "oslc.properties": properties,
        "oslc.paging": "true",
        "oslc.pageSize": str(page_size),
    }
    if where:
        params["oslc.where"] = where

    items: list[dict] = []
    next_url = url
    page = 0
    while next_url:
        page += 1
        resp = session.get(
            next_url,
            headers=headers,
            params=params if page == 1 else None,
            timeout=(15, timeout),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Work item query failed: HTTP {resp.status_code} - {resp.text[:300]}")
        data = resp.json()
        batch = data.get("oslc:results", [])
        items.extend(batch)
        print(f"  page {page}: +{len(batch)} (total {len(items)})")

        if limit and len(items) >= limit:
            return items[:limit]

        # OSLC 2.0 pagination: nextPage may be top-level or under responseInfo.
        next_url = data.get("oslc:nextPage")
        info = data.get("oslc:responseInfo")
        if not next_url and isinstance(info, dict):
            next_url = info.get("oslc:nextPage")
        if isinstance(next_url, dict):
            next_url = next_url.get("rdf:resource")
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="Retrieve EWM work items for a project area.")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="EWM ccm base URL")
    ap.add_argument("--project", default=os.getenv("EWM_PROJECT"), help="Project area name")
    ap.add_argument("--user", default=os.getenv("EWM_USER") or os.getenv("CID"), help="Username")
    ap.add_argument("--list", action="store_true", help="List all visible project areas and exit")
    ap.add_argument("--where", default=None, help="OSLC where filter, e.g. dcterms:type=\"defect\"")
    ap.add_argument("--csv", default=None, help="Write results to this CSV file")
    ap.add_argument("--page-size", type=int, default=100, help="OSLC page size (default 100)")
    ap.add_argument("--limit", type=int, default=None, help="Stop after this many work items")
    ap.add_argument("--timeout", type=int, default=120, help="Per-request read timeout seconds")
    ap.add_argument(
        "--properties",
        default="dcterms:identifier,dcterms:title,oslc_cm:status,dcterms:type",
        help="Comma-separated OSLC properties to retrieve",
    )
    args = ap.parse_args()

    if not args.list and not args.project:
        ap.error("--project is required (or set EWM_PROJECT), unless using --list")
    if not args.user:
        ap.error("--user is required (or set CID/EWM_USER)")

    password = os.getenv("EWM_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")

    alm_config.print_banner("work-item query (read-only)", commit=False)

    try:
        session = get_authenticated_session(args.server, args.user, password)

        if args.list:
            areas = list_project_areas(session, args.server)
            print(f"\n{len(areas)} visible project areas:\n" + "=" * 60)
            for name, uuid in areas:
                print(f"{name}  ->  {uuid}")
            return 0

        print(f"Resolving project area: '{args.project}'...")
        uuid = get_project_area_uuid(session, args.server, args.project)
        print(f"[OK] Project area UUID: {uuid}")

        print("Fetching work items...")
        items = fetch_work_items(
            session,
            args.server,
            uuid,
            args.properties,
            args.where,
            args.page_size,
            args.limit,
            args.timeout,
        )
        print(f"\nFound {len(items)} work items\n" + "=" * 60)

        rows = []
        for it in items:
            row = {
                "id": it.get("dcterms:identifier", "N/A"),
                "type": it.get("dcterms:type", ""),
                "status": it.get("oslc_cm:status", ""),
                "title": it.get("dcterms:title", ""),
            }
            rows.append(row)
            print(f"ID: {row['id']} | Type: {row['type']} | Status: {row['status']} | {row['title']}")

        if args.csv and rows:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["id", "type", "status", "title"])
                writer.writeheader()
                writer.writerows(rows)
            print(f"\n[OK] Wrote {len(rows)} rows to {args.csv}")
    except Exception as err:  # noqa: BLE001 - surface a clean message to the operator
        print(f"\n[ERROR] {err}")
        print("Ensure you are on the Chrysler intranet / VPN and your credentials are valid.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
