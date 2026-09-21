"""One-shot production identity gate for application release activation."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .migration_status import MigrationStatusError, check_migration_head
from .schema_identity import ALEMBIC_HEAD


class ReleasePreflightError(RuntimeError):
    """The configured release identity does not match the live authority."""


def check_release_preflight(
    *,
    database_url: str,
    expected_database: str,
    expected_schema_head: str,
    expected_release: str,
    expected_generation_id: str,
) -> dict[str, str]:
    if expected_schema_head != ALEMBIC_HEAD:
        raise ReleasePreflightError(
            "configured schema head does not match the candidate release"
        )
    try:
        check_migration_head(database_url)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                database = str(connection.scalar(text("SELECT current_database()")))
                generations = (
                    connection.execute(
                        text(
                            "SELECT generation_id, dish_release, status "
                            "FROM authority_generations WHERE status = 'active'"
                        )
                    )
                    .mappings()
                    .all()
                )
        finally:
            engine.dispose()
    except (MigrationStatusError, SQLAlchemyError) as exc:
        raise ReleasePreflightError("cannot prove live production identity") from exc
    if database != expected_database:
        raise ReleasePreflightError(
            "live database identity does not match configuration"
        )
    if len(generations) != 1:
        raise ReleasePreflightError(
            "live authority must have exactly one active generation"
        )
    generation = generations[0]
    observed = {
        "database": database,
        "schema_head": expected_schema_head,
        "dish_release": str(generation["dish_release"]),
        "generation_id": str(generation["generation_id"]),
        "generation_status": str(generation["status"]),
    }
    expected = {
        "database": expected_database,
        "schema_head": expected_schema_head,
        "dish_release": expected_release,
        "generation_id": expected_generation_id,
        "generation_status": "active",
    }
    if observed != expected:
        raise ReleasePreflightError(
            "live authority generation does not match configuration"
        )
    return observed


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ReleasePreflightError(f"{name} is required")
    return value


def main(env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    try:
        identity = check_release_preflight(
            database_url=_required(values, "DISH_PG_DATABASE_URL"),
            expected_database=_required(values, "DISH_PG_EXPECTED_DATABASE_NAME"),
            expected_schema_head=_required(values, "DISH_PG_EXPECTED_SCHEMA_HEAD"),
            expected_release=_required(values, "DISH_PG_EXPECTED_RELEASE"),
            expected_generation_id=_required(values, "DISH_PG_EXPECTED_GENERATION_ID"),
        )
    except ReleasePreflightError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 78
    print(json.dumps({"ok": True, "identity": identity}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
