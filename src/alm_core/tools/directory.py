"""Adding a user to the AD group through Microsoft Graph.

The alternative to the GPT web UI (``ALM_AD_DIRECTORY=graph``): one HTTPS call
from the run itself, no browser, no Windows worker. An app registration with
``GroupMember.ReadWrite.All`` (scoped by an administrative unit where the
tenant allows) authenticates with the client-credentials flow; its secret
comes from the secrets chain like every other secret.

Two facts decide whether this can be used at all:

* **Only groups mastered in Entra ID can be changed through Graph.** A group
  synced from on-premises AD is read-only there; Graph refuses with an
  on-premises-mastered error, which is reported as such. Those groups stay on
  GPT (or an on-premises directory API).
* **Membership is immediate.** Unlike GPT, which queues the change, a 204 from
  Graph means the user is in the group; the JazzUsers permission check still
  confirms access landed in Jazz.

Every call is recorded in the run's trace (``directory`` service), never with
the token. 429 and 5xx are retried with the server's ``Retry-After``.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import quote

import requests

from .. import trace
from ..errors import ConfigError, CredentialError, TransportError
from ..logging import get_logger
from ..models import Operation, ProvisionResult, RequestedUser
from .base import ToolContext, guarded_write, to_thread

log = get_logger("alm.tools.directory")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"
MAX_TRIES = 4
ON_PREM_HINTS = ("on-premises", "onpremises", "directory sync", "dirsync",
                 "mastered on-premises")


class GraphDirectory:
    """Microsoft Graph group membership for one tenant and app registration."""

    def __init__(self, settings, resolver=None, http: requests.Session | None = None):
        for name in ("graph_tenant_id", "graph_client_id"):
            if not getattr(settings, name, ""):
                raise ConfigError(f"ALM_AD_DIRECTORY=graph needs ALM_{name.upper()}")
        self.settings = settings
        self.resolver = resolver
        self.http = http or requests.Session()
        self.http.verify = True   # Graph is public; the corporate CA is not involved
        self._token: tuple[str, float] | None = None
        self._groups: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- plumbing

    def _secret(self) -> str:
        from ..credentials import build_resolver

        resolver = self.resolver or build_resolver(self.settings, interactive=False)
        try:
            return resolver.get(self.settings.graph_client_secret_name)
        except CredentialError as err:
            raise ConfigError(
                f"no Graph client secret: set the secret "
                f"{self.settings.graph_client_secret_name!r}") from err

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token[1]:
                return self._token[0]
            response = self.http.post(
                f"{LOGIN}/{self.settings.graph_tenant_id}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials",
                      "client_id": self.settings.graph_client_id,
                      "client_secret": self._secret(), "scope": SCOPE},
                timeout=30)
            if response.status_code != 200:
                raise ConfigError(
                    f"Graph sign-in failed (HTTP {response.status_code}): check the tenant, "
                    "client id and secret of the app registration")
            body = response.json()
            self._token = (body["access_token"],
                           time.time() + int(body.get("expires_in", 3600)) - 120)
            return self._token[0]

    def call(self, method: str, path: str, **kwargs) -> requests.Response:
        """One Graph call, retried on 429/5xx. Returns the final response."""
        headers = {"Authorization": f"Bearer {self.token()}", **kwargs.pop("headers", {})}
        response = None
        for attempt in range(1, MAX_TRIES + 1):
            with trace.span("directory", method.lower(), path=path.split("?")[0],
                            attempt=attempt) as step:
                try:
                    response = self.http.request(method, f"{GRAPH}{path}", headers=headers,
                                                 timeout=30, **kwargs)
                except requests.RequestException as err:
                    raise TransportError(f"Graph {method} {path} failed: {err}") from err
                step.update(status=response.status_code, ok=response.status_code < 400)
            if response.status_code != 429 and response.status_code < 500:
                return response
            wait = min(float(response.headers.get("Retry-After") or 2 ** attempt), 30.0)
            log.warning("graph_retry", status=response.status_code, wait=wait)
            time.sleep(wait)
        return response

    @staticmethod
    def _error(response: requests.Response) -> str:
        try:
            return response.json().get("error", {}).get("message", "") or response.text[:300]
        except ValueError:
            return response.text[:300]

    # -------------------------------------------------------------- lookups

    def user_id(self, userid: str) -> str | None:
        if self.settings.graph_user_lookup == "upn":
            upn = f"{userid}@{self.settings.graph_upn_suffix}"
            response = self.call("GET", f"/users/{quote(upn)}?$select=id")
            return response.json().get("id") if response.status_code == 200 else None
        safe = userid.replace("'", "''")
        response = self.call(
            "GET", f"/users?$filter=onPremisesSamAccountName eq '{quote(safe)}'"
                   "&$select=id&$count=true", headers={"ConsistencyLevel": "eventual"})
        if response.status_code != 200:
            raise TransportError(f"Graph user lookup failed: {self._error(response)}")
        found = response.json().get("value", [])
        return found[0]["id"] if len(found) == 1 else None

    def group_id(self, group: str) -> str:
        if self.settings.graph_group_id:
            return self.settings.graph_group_id
        if group not in self._groups:
            safe = group.replace("'", "''")
            response = self.call("GET", f"/groups?$filter=displayName eq '{quote(safe)}'"
                                        "&$select=id")
            found = response.json().get("value", []) if response.status_code == 200 else []
            if len(found) != 1:
                raise ConfigError(f"group {group!r} is not found (or not unique) in Entra "
                                  "ID; set ALM_GRAPH_GROUP_ID")
            self._groups[group] = found[0]["id"]
        return self._groups[group]

    # ------------------------------------------------------------ the write

    def add_member(self, *, userid: str, group: str, domain: str = "") -> tuple[bool, str]:
        """Add one user. ``(True, ...)`` when they are a member afterwards."""
        del domain  # Entra ID identifies the user; GPT's domain field has no role here
        member = self.user_id(userid)
        if member is None:
            return False, f"{userid} is not found (or not unique) in Entra ID"
        response = self.call(
            "POST", f"/groups/{self.group_id(group)}/members/$ref",
            json={"@odata.id": f"{GRAPH}/directoryObjects/{member}"})
        if response.status_code == 204:
            return True, f"{userid} added to {group} through Microsoft Graph"
        message = self._error(response)
        if response.status_code == 400 and "already exist" in message.lower():
            return True, f"{userid} was already a member of {group}"
        if any(hint in message.lower() for hint in ON_PREM_HINTS):
            raise ConfigError(
                f"{group} is mastered in on-premises AD, so Microsoft Graph cannot change "
                "it. Use ALM_AD_DIRECTORY=gpt for this group.")
        if response.status_code in (401, 403):
            return False, ("the app registration may not change this group's members "
                           f"(HTTP {response.status_code}): {message}")
        return False, f"Graph refused the change (HTTP {response.status_code}): {message}"


_directories: dict[int, GraphDirectory] = {}


def directory_for(settings) -> GraphDirectory:
    key = id(settings)
    if key not in _directories:
        _directories[key] = GraphDirectory(settings)
    return _directories[key]


async def request_group_membership(ctx: ToolContext, user: RequestedUser, *,
                                   group: str, domain: str,
                                   directory: GraphDirectory | None = None) -> ProvisionResult:
    """Add one user through Graph, under the ledger, the approval and the audit."""
    directory = directory or directory_for(ctx.settings)
    work_item_id = user.work_item_ids[0] if user.work_item_ids else ""

    async def add() -> tuple[bool, str, dict]:
        ok, message = await to_thread(directory.add_member, userid=user.userid,
                                      group=group, domain=domain)
        return ok, message, {"group": group, "via": "graph"}

    return await guarded_write(
        ctx, userid=user.userid, work_item_id=work_item_id,
        operation=Operation.AD_GROUP_ADD, step="ad_provision", action=add)
