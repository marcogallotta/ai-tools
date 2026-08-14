"""Service-manager entrypoint for fail-closed PostgreSQL shadow-worker startup."""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from dish_tool.startup_exit import (
    NON_RETRYABLE_STARTUP_EXIT_STATUS,
    RETRYABLE_STARTUP_EXIT_STATUS,
)

from . import shadow_worker
from .migration_status import MigrationStatusError, check_migration_head

LOGGER = logging.getLogger("dish.pg.shadow")


def _startup_binding(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    args, _unknown = parser.parse_known_args(argv)
    return args


def _preflight(argv: list[str] | None) -> int | None:
    args = _startup_binding(argv)
    selected = make_url(args.database_url)
    if selected.get_backend_name() != "postgresql":
        LOGGER.error("shadow worker requires PostgreSQL")
        return NON_RETRYABLE_STARTUP_EXIT_STATUS
    if selected.database != args.expected_database_name:
        LOGGER.error("PostgreSQL URL database does not match expected database name")
        return NON_RETRYABLE_STARTUP_EXIT_STATUS
    try:
        check_migration_head(args.database_url)
        engine = create_engine(args.database_url, future=True)
        try:
            with engine.connect() as connection:
                database_name = str(connection.scalar(text("SELECT current_database()")))
        finally:
            engine.dispose()
    except MigrationStatusError as exc:
        LOGGER.error("shadow worker schema startup blocked: %s", exc)
        return NON_RETRYABLE_STARTUP_EXIT_STATUS
    except SQLAlchemyError as exc:
        LOGGER.warning(
            "shadow worker database unavailable during startup: %s", type(exc).__name__
        )
        return RETRYABLE_STARTUP_EXIT_STATUS
    if database_name != args.expected_database_name:
        LOGGER.error("connected PostgreSQL database does not match expected database name")
        return NON_RETRYABLE_STARTUP_EXIT_STATUS
    return None


def main(argv: list[str] | None = None) -> int:
    blocked = _preflight(argv)
    if blocked is not None:
        return blocked
    try:
        return shadow_worker.main(argv)
    except SQLAlchemyError as exc:
        LOGGER.warning(
            "shadow worker database unavailable during startup: %s", type(exc).__name__
        )
        return RETRYABLE_STARTUP_EXIT_STATUS


if __name__ == "__main__":
    raise SystemExit(main())
