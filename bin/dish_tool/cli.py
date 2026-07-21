"""JSON-only argparse surface for the agent-facing ``dish`` executable."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .backend import AsanaBackend
from .commands import DishApplication
from .constants import DEFAULT_DB_PATH, DEFAULT_PROTOCOL_WORKTREE
from .database import initialize_database
from .errors import DishRuleError
from .releases import resolve_release
from .results import error_envelope, exit_status


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DishRuleError(
            "INVALID_ARGUMENT", message, rule="invalid_arguments"
        )


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="dish")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    create.add_argument("--title", required=True)

    read = subparsers.add_parser("read")
    read.add_argument("task_gid")
    read.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("submission_id")
    inspect.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    start = subparsers.add_parser("start")
    start.add_argument("task_gid")
    start.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    start.add_argument("--kind", required=True, choices=("planning", "initial", "change"))
    start.add_argument("--change-level", choices=("small", "large"))
    start.add_argument("--change-reason")
    return parser


def build_application() -> DishApplication:
    db_path = Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
    release_root = Path(
        os.environ.get("DISH_PROTOCOL_WORKTREE", str(DEFAULT_PROTOCOL_WORKTREE))
    ).expanduser()
    conn = initialize_database(db_path)
    return DishApplication(
        conn,
        AsanaBackend(),
        release_loader=lambda: resolve_release(release_root),
    )


def _argument_context(argv: Sequence[str]) -> dict[str, str | None]:
    command = argv[0] if argv and not argv[0].startswith("-") else "unknown"
    agent = None
    if "--agent" in argv:
        index = argv.index("--agent")
        if index + 1 < len(argv):
            agent = argv[index + 1]
    task_gid = None
    submission_id = None
    if command in {"read", "start"} and len(argv) > 1 and not argv[1].startswith("-"):
        task_gid = argv[1]
    if command == "inspect" and len(argv) > 1 and not argv[1].startswith("-"):
        submission_id = argv[1]
    return {
        "command": command,
        "agent": agent,
        "task_gid": task_gid,
        "submission_id": submission_id,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    application: DishApplication | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    context = _argument_context(arguments)
    owned_application = application is None
    try:
        app = application or build_application()
    except Exception:
        error = DishRuleError(
            "INTERNAL_ERROR",
            "dish failed during startup",
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
                agent=context["agent"],
                task_gid=context["task_gid"],
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
