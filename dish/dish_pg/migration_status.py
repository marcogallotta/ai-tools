"""Fast fail-closed PostgreSQL startup check against the exact ALEMBIC_HEAD.

The shadow worker runs this inside its main process before it touches runtime
state. Deterministic schema drift therefore remains a startup refusal while the
service manager can distinguish it from transient database unavailability.

This checks only the Alembic version marker, not row content — unlike
`dish_pg.operations_evidence.fingerprint_database`, which hashes every row of
every table and is meant for deliberate release/cutover evidence, not a cheap
startup gate.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from dish_tool.startup_exit import NON_RETRYABLE_STARTUP_EXIT_STATUS

from .release import ALEMBIC_HEAD

REPO_ROOT = Path(__file__).resolve().parents[1]


class MigrationStatusError(RuntimeError):
    """The target database's applied schema head does not match ALEMBIC_HEAD."""


def check_migration_head(database_url: str) -> str:
    """Return the target's current Alembic head, raising if it isn't ALEMBIC_HEAD."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "dish_pg/migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    script = ScriptDirectory.from_config(cfg)
    script_heads = set(script.get_heads())
    if script_heads != {ALEMBIC_HEAD}:
        raise MigrationStatusError(
            f"dish_pg.release.ALEMBIC_HEAD ({ALEMBIC_HEAD!r}) does not match the migration "
            f"script directory's own head(s) ({sorted(script_heads)!r}); fix ALEMBIC_HEAD "
            "before trusting this check"
        )
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            current_heads = context.get_current_heads()
    finally:
        engine.dispose()
    if current_heads != (ALEMBIC_HEAD,):
        raise MigrationStatusError(
            f"database is at {current_heads!r}, code expects {ALEMBIC_HEAD!r}; "
            "run the reviewed `dish-pg-migrate --check/--apply` release gate "
            "for this exact environment and source before starting any dependent service"
        )
    return ALEMBIC_HEAD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args(argv)
    try:
        head = check_migration_head(args.database_url)
    except MigrationStatusError as exc:
        sys.stderr.write(f"dish-pg-migration-status: {exc}\n")
        return NON_RETRYABLE_STARTUP_EXIT_STATUS
    sys.stdout.write(f"dish-pg-migration-status: ok, database is at {head}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
