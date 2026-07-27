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
from .backend import AsanaBackend
from .releases import configured_honest_path, resolve_release
from dish_service.client import DishAdminServiceClient
from .errors import DishRuleError
from .results import error_envelope, exit_status

_ADMIN_COMMANDS = {"recover", "discard", "migrate", "reopen", "supply-evidence", "record-human-decision", "authorize-governed-change", "recover-lease", "backup-create", "backup-restore"}
_OPERATION_ADMIN_COMMANDS = {"recover", "discard", "reopen", "supply-evidence", "record-human-decision", "authorize-governed-change", "recover-lease"}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            message,
            rule="invalid_arguments",
        )


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="dish-admin",
        description=(
            "Marco-only recovery and override commands for the dish tool. Agents do not run "
            "these; they exist for reconciling an interrupted write/movement, discarding a "
            "stale operation, clearing a stuck state, or reopening a two-pass Human Review hold."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    recover = subparsers.add_parser(
        "recover",
        help="reconcile an interrupted write/movement against a fresh live Asana reread",
    )
    recover.add_argument("submission_id")
    recover.add_argument(
        "--outcome",
        required=True,
        choices=("not-applied", "applied"),
        help="record only what the live reread proves; a contradictory outcome fails closed",
    )
    recover.add_argument("--reason", required=True)

    discard = subparsers.add_parser("discard", help="abandon a stale open operation without applying it")
    discard.add_argument("submission_id")
    discard.add_argument("--reason", required=True)

    reopen = subparsers.add_parser(
        "reopen",
        help="the only path out of the two-pass Verification Human Review hold",
    )
    reopen.add_argument("submission_id")
    reopen.add_argument(
        "--category",
        required=True,
        choices=("evidence", "premise", "method", "scope"),
        help="what concretely changed to make another Verification cycle worthwhile",
    )
    reopen.add_argument("--before", required=True)
    reopen.add_argument("--after", required=True)
    reopen.add_argument("--editor", required=True, choices=("claude", "gpt", "codex"))
    reopen.add_argument("--model", required=True)
    reopen.add_argument("--run-id", required=True)
    reopen.add_argument("--file", dest="file_path", required=True)
    reopen.add_argument("--date", required=True)

    migrate = subparsers.add_parser(
        "migrate", help="migrate one individually encountered older-schema task after cutover"
    )
    migrate.add_argument("task_gid")

    recover_lease = subparsers.add_parser(
        "recover-lease", help="reclaim an expired service lease before an admin operation"
    )
    recover_lease.add_argument("submission_id")
    recover_lease.add_argument("--reason", required=True)

    backup_create = subparsers.add_parser(
        "backup-create", help="create a validated online snapshot of the shared database"
    )
    backup_create.add_argument("--label", default="manual")

    backup_restore = subparsers.add_parser(
        "backup-restore", help="restore a managed shared-database snapshot"
    )
    backup_restore.add_argument("backup_id")

    authorize = subparsers.add_parser(
        "authorize-governed-change", help="authorize a single field change the tool would otherwise block"
    )
    authorize.add_argument("submission_id")
    authorize.add_argument("--field", required=True)
    authorize.add_argument("--before", required=True, type=json.loads, help="typed JSON value before the change")
    authorize.add_argument("--after", required=True, type=json.loads, help="typed JSON value after the change")
    authorize.add_argument("--reason", required=True)
    authorize.add_argument("--run-id")

    _hold_help = {
        "supply-evidence": "resume a pending-evidence operation with Marco-supplied evidence",
        "record-human-decision": "resume a pending-human-review operation with Marco's recorded decision",
    }
    for name, help_text in _hold_help.items():
        hold = subparsers.add_parser(name, help=help_text)
        hold.add_argument("submission_id")
        hold.add_argument("--detail", required=True)
        hold.add_argument("--resume-status", required=True, choices=("pending-research", "pending-verification"))
        hold.add_argument("--file", dest="file_path")
        hold.add_argument("--editor", choices=("claude", "gpt", "codex"))
        hold.add_argument("--model")
        hold.add_argument("--run-id")
    return parser


def build_application():
    mode = os.environ.get("DISH_MODE", "").strip().lower()
    service_url = os.environ.get("DISH_SERVICE_URL", "").strip()
    if mode not in {"", "local", "service"}:
        raise DishRuleError("INVALID_ARGUMENT", "DISH_MODE must be local or service", rule="dish_mode_invalid")
    if mode == "service" or (not mode and service_url):
        if not service_url:
            raise DishRuleError("INVALID_ARGUMENT", "DISH_SERVICE_URL is required in service mode", rule="service_url_required")
        return DishAdminServiceClient(
            service_url,
            token=os.environ.get("DISH_ADMIN_TOKEN", ""),
            run_id=os.environ.get("DISH_CLIENT_RUN_ID", ""),
            timeout=float(os.environ.get("DISH_SERVICE_CLIENT_TIMEOUT", "65")),
        )
    if os.environ.get("DISH_LIVE_MODE", "").strip().lower() in {"1", "true", "yes"}:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "live mode requires the shared dish service",
            rule="shared_service_required",
        )
    db_path = Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
    honest_root = configured_honest_path()
    return DishAdminApplication(initialize_database(db_path), backend=AsanaBackend(), release_loader=lambda: resolve_release(honest_root, include_migrations=True))


def _argument_context(argv: Sequence[str]) -> dict[str, str | None]:
    command = argv[0] if argv and not argv[0].startswith("-") else "unknown"
    submission_id = None
    task_gid = None
    if (
        command in _OPERATION_ADMIN_COMMANDS
        and len(argv) > 1
        and not argv[1].startswith("-")
    ):
        submission_id = argv[1]
    if command == "migrate" and len(argv) > 1 and not argv[1].startswith("-"):
        task_gid = argv[1]
    return {"command": command, "submission_id": submission_id, "task_gid": task_gid}


def main(
    argv: Sequence[str] | None = None,
    *,
    application: DishAdminApplication | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    if "-h" in arguments or "--help" in arguments:
        build_parser().parse_args(arguments)  # prints help and raises SystemExit(0)

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
            if command == "recover-lease":
                method = getattr(app, "recover_lease", None)
                if method is None:
                    result = error_envelope(
                        command,
                        DishRuleError(
                            "PROTOCOL_INCOMPATIBLE",
                            "service lease recovery requires shared-service mode",
                            rule="shared_service_required",
                        ),
                        submission_id=parsed["submission_id"],
                    )
                else:
                    result = method(parsed["submission_id"], reason=parsed["reason"])
            elif command == "backup-create":
                method = getattr(app, "create_backup", None)
                if method is None:
                    result = error_envelope(
                        command,
                        DishRuleError(
                            "PROTOCOL_INCOMPATIBLE",
                            "shared database backup requires shared-service mode",
                            rule="shared_service_required",
                        ),
                    )
                else:
                    result = method(label=parsed["label"])
            elif command == "backup-restore":
                method = getattr(app, "restore_backup", None)
                if method is None:
                    result = error_envelope(
                        command,
                        DishRuleError(
                            "PROTOCOL_INCOMPATIBLE",
                            "shared database restore requires shared-service mode",
                            rule="shared_service_required",
                        ),
                    )
                else:
                    result = method(parsed["backup_id"])
            else:
                result = app.execute(command, **parsed)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])
    finally:
        if owned_application:
            conn = getattr(app, "conn", None)
            if conn is not None:
                conn.close()
