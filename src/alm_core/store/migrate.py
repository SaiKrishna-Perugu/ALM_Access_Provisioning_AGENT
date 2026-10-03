"""Bring the database's schema up to this code's version, as a step of its own.

    python -m alm_core.store.migrate            # migrate, then report the version
    python -m alm_core.store.migrate --check    # report only; exit 1 if behind

By default every service migrates the schema when it starts
(``ALM_AUTO_MIGRATE=true``), which is right for one service on a database it
owns. Where the services connect as a role that may read and write but not
change the schema (the grants from ``python -m alm_core.store.admin``), set
``ALM_AUTO_MIGRATE=false`` on them and run this once per release, as the
owner, before the new revision takes traffic. A service started against a
schema older than its code then refuses to start, naming this command, rather
than failing on its first query.

Migrations only add: a table, a column, an index or a trigger. They are safe
to run while the previous release is still serving.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from ..errors import ConfigError


async def migrate(settings, *, check_only: bool = False) -> dict:
    """Migrate (or check) the store, agent memory and run checkpoints."""
    from . import get_store

    if not (settings.postgres_dsn or getattr(settings, "ledger_path", "")):
        raise ConfigError("no database configured: set ALM_POSTGRES_DSN (or ALM_LEDGER_PATH)")
    settings = settings.model_copy(update={"auto_migrate": not check_only})
    store = await get_store(settings)
    try:
        current = await store.schema_version()
        if not check_only:
            from alm_agents.graph import checkpointer_for
            from alm_agents.memory import MemoryStore

            await MemoryStore(store).migrate()
            async with checkpointer_for(settings):
                pass  # its setup() creates or upgrades the checkpoint tables
        return {"version": current, "expected": _expected(store)}
    finally:
        await store.close()


def _expected(store) -> int:
    module = sys.modules[type(store).__module__]
    return int(module.SCHEMA_VERSION)


def main(argv: list[str] | None = None) -> int:
    from ..config import get_settings
    from ..logging import configure

    parser = argparse.ArgumentParser(prog="python -m alm_core.store.migrate",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="report the schema version; exit 1 if it is behind the code")
    args = parser.parse_args(argv)
    configure()
    try:
        result = asyncio.run(migrate(get_settings(), check_only=args.check))
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    behind = result["version"] < result["expected"]
    print(f"schema version {result['version']} (this code: {result['expected']})"
          + (" - behind: run without --check" if behind else ""))
    return 1 if behind else 0


if __name__ == "__main__":
    raise SystemExit(main())
