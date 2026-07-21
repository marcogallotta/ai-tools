"""JSON-only argparse surface for the Marco-only ``dish-admin`` executable."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .admin import DishAdminApplication
from .constants import DEFAULT_DB_PATH
from .database import initialize_database
from .errors import DishRuleError
from .results import error_envelope, exit_status


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            message,
            rule="invalid_arguments",
        )


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="dish-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    unblock = subparsers.add_parser("unblock")
    unblock.add_argument("submission_id")
    unblock.add_argument("--reason", required=True)
    return parser


def build_application() -> DishAdminApplication:
    db_path = Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
    return DishAdminApplication(initialize_database(db_path))


def _argument_context(argv: Sequence[str]) -> dict[str, str | None]:
    command = argv[0] if argv and not argv[0].startswith("-") else "unknown"
    submission_id = None
    if command == "unblock" and len(argv) > 1 and not argv[1].startswith("-"):
        submission_id = argv[1]
    return {"command": command, "submission_id": submission_id}


def main(
    argv: Sequence[str] | None = None,
    *,
    application: DishAdminApplication | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    context = _argument_context(arguments)
    owned_application = application is None
    try:
        app = application or build_application()
    except Exception:
        error = DishRuleError(
            "INTERNAL_ERROR",
            "dish-admin failed during startup",
            rule="startup_failure",
        )
        result = error_envelope(context["command"] or "unknown", error)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])

    try:
        try:
            parsed = vars(build_parser().parse_args(arguments))
        except DishRuleError as exc:
            result = app.record_argument_failure(
                context["command"] or "unknown",
                exc,
                submission_id=context["submission_id"],
            )
        else:
            command = parsed.pop("command")
            result = app.execute(command, **parsed)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])
    finally:
        if owned_application:
            app.conn.close()
