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
from .constants import DEFAULT_DB_PATH
from .database import initialize_database
from .errors import DishRuleError
from .releases import configured_honest_path, resolve_release
from .results import error_envelope, exit_status


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DishRuleError(
            "INVALID_ARGUMENT", message, rule="invalid_arguments"
        )


def _add_title_declaration(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dish-name")
    parser.add_argument("--recognition")
    parser.add_argument("--role", dest="roles", action="append")
    parser.add_argument("--no-role-tags", action="store_true")
    parser.add_argument("--blocker", dest="blockers", action="append")
    parser.add_argument("--no-blockers", action="store_true")


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="dish")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    create.add_argument("--title", required=True)

    sections = subparsers.add_parser("sections")
    sections.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    read = subparsers.add_parser("read")
    read.add_argument("task_gid")
    read.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("submission_id")
    inspect.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    start = subparsers.add_parser("start")
    start.add_argument("task_gid")
    start.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    start.add_argument("--kind", required=True, choices=("planning", "initial", "change", "verification"))
    start.add_argument("--run-id")
    start.add_argument("--independence-attestation")
    start.add_argument("--change-level", choices=("small", "large"))
    start.add_argument("--change-reason")

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("submission_id")
    prepare.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    prepare.add_argument("--file", dest="file_path", required=True)
    prepare.add_argument("--exemption-revision")
    prepare.add_argument("--material-classification", choices=("material", "non-material"))
    _add_title_declaration(prepare)

    approve = subparsers.add_parser("approve")
    approve.add_argument("submission_id")
    approve.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    approve.add_argument("--file", dest="file_path")
    approve.add_argument("--correction", required=True, choices=("none", "small"))
    approve.add_argument("--reviewed-identity")
    approve.add_argument("--run-id")
    approve.add_argument("--independence-attestation")
    approve.add_argument("--semantic-review-complete", action="store_true")
    approve.add_argument("--provenance-complete", action="store_true")
    _add_title_declaration(approve)

    reject = subparsers.add_parser("reject")
    reject.add_argument("submission_id")
    reject.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    reject.add_argument("--reason", required=True)
    reject.add_argument("--route", choices=("large", "evidence", "human-review"))
    reject.add_argument("--file", dest="file_path")
    reject.add_argument("--resume-status", choices=("pending-verification", "pending-research"))
    reject.add_argument("--changed-since-prior")
    reject.add_argument("--take-ownership", action="store_true")
    reject.add_argument("--run-id")
    reject.add_argument("--independence-attestation")

    submit = subparsers.add_parser("submit")
    submit.add_argument("submission_id")
    submit.add_argument("--file", dest="file_path")
    return parser


def build_application() -> DishApplication:
    db_path = Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
    honest_root = configured_honest_path()
    conn = initialize_database(db_path)
    return DishApplication(
        conn,
        AsanaBackend(),
        release_loader=lambda role=None: resolve_release(
            honest_root, protocol_role=role
        ),
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
    if (
        command in {"inspect", "prepare", "approve", "reject", "submit"}
        and len(argv) > 1
        and not argv[1].startswith("-")
    ):
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
    except DishRuleError as exc:
        result = error_envelope(context["command"] or "unknown", exc)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])
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
