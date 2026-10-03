"""Database administration that the application itself must not be able to do.

    python -m alm_core.store.admin grants --app-role alm_app

prints the SQL a database administrator runs, once, so that the role the
services connect as can read and write its tables but can never change or
delete an audit row. It only prints: applying it needs the owner's or an
administrator's login, which this code never holds.

It matters when migrations run as a separate owner role (the migration job in
the deployment pipeline). An application role that owns the tables could
grant itself the rights back, so the services must not connect as the owner.
Until then the append-only trigger (schema version 4) still refuses UPDATE and
DELETE on ``alm_audit`` for every role, owner included; dropping it is DDL,
which the database's own audit log records.
"""
from __future__ import annotations

import argparse
import re
import sys

# Every table the services read and write. The audit table is not here: it
# gets INSERT and SELECT only.
APP_TABLES = ("alm_idempotency", "alm_approval", "alm_schema_version", "alm_run",
              "alm_run_job", "alm_run_control", "alm_webhook_seen", "alm_lease",
              "alm_trace_event", "alm_approval_vote", "alm_agent_memory")
_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_@.\-]{0,62}$")


def grants_sql(app_role: str, schema: str = "public") -> str:
    """The grants for ``app_role``. Raises ValueError on a malformed name."""
    for name, value in (("role", app_role), ("schema", schema)):
        if not _ROLE.match(value):
            raise ValueError(f"not a valid {name} name: {value!r}")
    role = f'"{app_role}"'
    lines = [
        "-- Run as the tables' owner or an administrator, after the migrations.",
        "BEGIN;",
        f"GRANT USAGE ON SCHEMA {schema} TO {role};",
        *(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.{t} TO {role};"
          for t in APP_TABLES),
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role};",
        "-- The audit trail: written and read, never changed or removed.",
        f"REVOKE ALL ON {schema}.alm_audit FROM {role};",
        f"GRANT SELECT, INSERT ON {schema}.alm_audit TO {role};",
        "COMMIT;",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alm_core.store.admin",
        description="Print administrator SQL for the ALM store. Never connects.")
    sub = parser.add_subparsers(dest="command", required=True)
    grants = sub.add_parser("grants", help="the application role's grants")
    grants.add_argument("--app-role", required=True,
                        help="the role (or IAM database user) the services connect as")
    grants.add_argument("--schema", default="public")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(grants_sql(args.app_role, args.schema))
    except ValueError as err:
        parser.error(str(err))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
