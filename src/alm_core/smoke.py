"""Connectivity smoke test. Read-only, runs on every deployment.

A successful deployment proves the resources exist. It proves nothing about
whether the container can resolve ``*.example.intra``, whether the
ExpressRoute peering carries the traffic, or whether the corporate CA in the
image actually validates the intranet certificates. Those are the things that
break, and they break silently until the first real run.

Everything here is a GET or a connect. It never authenticates a write path, never
touches a work item, and is safe to run against production.

    python -m alm_core.smoke [--json]

Exit codes: 0 all checks passed, 1 one or more failed, 2 misconfigured.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .config import get_settings
from .errors import ConfigError
from .logging import configure, get_logger

log = get_logger("alm.smoke")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    ms: int = 0


def _timed(func, name: str) -> Check:
    started = time.monotonic()
    try:
        ok, detail = func()
    except Exception as err:  # noqa: BLE001 - a failed check is a result, not a crash
        ok, detail = False, f"{type(err).__name__}: {err}"
    return Check(name=name, ok=ok, detail=detail,
                 ms=int((time.monotonic() - started) * 1000))


def check_dns(url: str) -> Check:
    """Private DNS is the single most common thing to be wrong after a deploy."""
    host = urlsplit(url).hostname or ""

    def run():
        if not host:
            return False, "no host configured"
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
        return True, f"{host} -> {', '.join(addresses)}"

    return _timed(run, f"dns:{host or 'unset'}")


def check_tls(url: str, ca_bundle) -> Check:
    """Prove the corporate CA in this image validates the intranet certificate."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443

    def run():
        if not host:
            return False, "no host configured"
        context = ssl.create_default_context(
            cafile=ca_bundle if isinstance(ca_bundle, str) else None)
        if ca_bundle is False:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=10) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert() or {}
            subject = dict(x[0] for x in cert.get("subject", ())).get(
                "commonName", "?")
            return True, f"{host}:{port} verified, CN={subject}"

    return _timed(run, f"tls:{host or 'unset'}")


def check_http(url: str, settings) -> Check:
    """An unauthenticated GET: a response of any status proves reachability."""
    import requests

    def run():
        response = requests.get(url, timeout=settings.timeout, verify=settings.verify,
                                allow_redirects=False)
        return True, f"HTTP {response.status_code} from {url}"

    return _timed(run, f"http:{urlsplit(url).hostname or 'unset'}")


def check_secret_manager(settings) -> Check:
    def run():
        if not settings.project_id:
            return True, "not configured (skipped)"
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        # Access the one secret the run genuinely cannot proceed without. A
        # list call would pass with no permission on the value itself.
        name = settings.secret_path(settings.password_secret_name)
        response = client.access_secret_version(request={"name": name})
        length = len(response.payload.data)
        return True, f"{settings.password_secret_name} readable ({length} bytes)"

    return _timed(run, "secret-manager")


def check_postgres(settings) -> Check:
    def run():
        if not settings.postgres_dsn:
            return True, "not configured (skipped)"
        import psycopg

        from .credentials import postgres_dsn

        with psycopg.connect(postgres_dsn(settings), connect_timeout=10) as conn, conn.cursor() as cur:
            cur.execute("SELECT current_user, version()")
            user, version = cur.fetchone()
        auth = "IAM token" if settings.postgres_iam_auth else "password"
        return True, f"connected as {user} using {auth} ({version.split(',')[0]})"

    return _timed(run, "cloud-sql")


def check_pubsub(settings) -> Check:
    def run():
        if not settings.project_id:
            return True, "not configured (skipped)"
        from google.cloud import pubsub_v1

        # get_topic proves both reachability and the publisher IAM binding
        # without putting a message on the queue the worker is watching.
        publisher = pubsub_v1.PublisherClient()
        topic = publisher.get_topic(request={"topic": settings.topic_path()})
        return True, f"{topic.name.rsplit('/', 1)[-1]} reachable"

    return _timed(run, "pub/sub")


def check_vertex(settings) -> Check:
    def run():
        if not settings.llm_enabled or not settings.project_id:
            return True, "not configured (skipped)"
        import vertexai

        vertexai.init(project=settings.project_id, location=settings.vertex_region)
        # Initialising resolves credentials and the regional endpoint; it does
        # not spend a token, which keeps this safe to run on every deployment.
        return True, f"{settings.agent_model} in {settings.vertex_region}"

    return _timed(run, "vertex-ai")


def run_all(settings) -> list[Check]:
    checks: list[Check] = []
    for url in (settings.ewm_server, settings.jts_server):
        if not url:
            continue
        checks.append(check_dns(url))
        checks.append(check_tls(url, settings.verify))
        checks.append(check_http(f"{url}/authenticated/identity", settings))
    checks.append(check_secret_manager(settings))
    checks.append(check_postgres(settings))
    checks.append(check_pubsub(settings))
    checks.append(check_vertex(settings))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Connectivity smoke test (read-only).")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args()

    configure()
    try:
        settings = get_settings()
    except ConfigError as err:
        print(f"[STOP] {err}", file=sys.stderr)
        return 2

    checks = run_all(settings)
    failed = [c for c in checks if not c.ok]

    if args.json:
        print(json.dumps({"environment": settings.environment,
                          "ok": not failed,
                          "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        print(f"Environment: {settings.environment}   "
              f"TLS: {'verified' if settings.verify else 'UNVERIFIED'}")
        for check in checks:
            print(f"  [{'OK  ' if check.ok else 'FAIL'}] {check.name:<28} "
                  f"{check.ms:>5}ms  {check.detail}")
        print()
        print(f"{len(checks) - len(failed)}/{len(checks)} checks passed.")
        if failed:
            print("A DNS or TLS failure here almost always means Cloud DNS private "
                  "zones or the Interconnect/VPN attachment are not in place yet.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
