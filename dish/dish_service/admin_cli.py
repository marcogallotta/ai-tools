"""Human-first argparse surface for the environment-scoped ``dish-admin`` executable."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Sequence

from dish_tool.admin import DishAdminApplication
from dish_tool.client_profiles import (
    add_profile_argument,
    argv_without_profile,
    profile_from_argv,
    resolve_client_profile,
)
from dish_tool.constants import DB_PATH
from dish_tool.database import initialize_database
from dish_tool.backend import AsanaBackend
from dish_tool.admin_command_spec import (
    ADMIN_COMMANDS as _ADMIN_COMMANDS,
    ADMIN_COMMAND_SPECS,
    RESOLVED_OPERATION_TARGET_COMMANDS as _OPERATION_ADMIN_COMMANDS,
)
from dish_tool.releases import configured_honest_path, resolve_release
from dish_service.client import DishAdminServiceClient
from dish_service.database_ownership import ServiceDatabaseOwnership, database_process_lock_path
from dish_service.process_lock import DatabaseProcessLock
from dish_service.identifiers import require_asana_gid, require_dish_uuid
from dish_service.task_urls import task_gid_from_url
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope, exit_status
from dish_tool.admin_human import render_admin_result



class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            message,
            rule="invalid_arguments",
        )


def _admin_name(name: str) -> str:
    return ADMIN_COMMAND_SPECS[name].name


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="dish-admin",
        description=(
            "Human administration for blocked or exceptional Dish workflows. Start with "
            "`dish-admin inspect TASK_OR_OPERATION`; use low-level mutation commands only when "
            "Dish returns them as the safe next action. Agents may use this tool only against "
            "the test profile; production administration remains Marco-only."
        ),
        epilog=(
            "Common recovery rule: recover-lease lets the same durable agent run continue; "
            "expire-lease only releases an active lease; abandon-operation is for a run that "
            "will not return. None of these commands approves or submits a dish."
        ),
    )
    add_profile_argument(parser)
    parser.add_argument(
        "--json", action="store_true",
        help="emit the raw machine-readable response instead of human terminal output",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="include technical rule and response details in human output",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        _admin_name("attention"),
        help=(
            "scan all active Dish workflow records for stale ownership, abandonments, "
            "holds, and uncertain effects without changing anything"
        ),
    )

    review_queue = subparsers.add_parser(
        _admin_name("review-queue"),
        help="list durable semantic proposals and Human Review items waiting for Marco",
    )
    review_queue.add_argument(
        "--status", choices=("active", "pending", "approved", "all"), default="active"
    )

    review_inspect = subparsers.add_parser(
        _admin_name("review-inspect"),
        help="show one review item by UUID or current queue number",
    )
    review_inspect.add_argument("proposal_id")

    review_approve = subparsers.add_parser(
        _admin_name("review-approve"),
        help="approve a semantic bundle or resolve a Human Review item",
    )
    review_approve.add_argument("proposal_id")
    review_approve.add_argument(
        "--reason", default="Approved after reviewing the exact linked change bundle."
    )
    review_approve.add_argument(
        "--detail",
        help="for Human Review items, Marco's complete decision and reasoning",
    )

    review_reject = subparsers.add_parser(
        _admin_name("review-reject"),
        help="reject a pending semantic bundle or dismiss an unanswered Human Review escalation",
    )
    review_reject.add_argument("proposal_id")
    review_reject.add_argument("--reason", required=True)

    subparsers.add_parser(
        _admin_name("holds"), help="list every currently open Evidence or Human Review hold"
    )

    _submission_target_help = (
        "exact operation ID, task GID, or supported Asana task URL "
        "(a task GID/URL resolves to that task's open operation)"
    )

    inspect_admin = subparsers.add_parser(
        _admin_name("inspect"),
        help="explain what a task is waiting on and show Marco's safe next actions",
    )
    inspect_admin.add_argument("submission_id", help=_submission_target_help)

    recover = subparsers.add_parser(
        _admin_name("recover"),
        help=(
            "reconcile an uncertain external write or movement from a fresh live reread; "
            "this is not lease recovery"
        ),
    )
    recover.add_argument("submission_id", help=_submission_target_help)
    recover.add_argument(
        "--outcome",
        required=True,
        choices=("inspect", "not-applied", "applied"),
        help="record only what the live reread proves; a contradictory outcome fails closed",
    )
    recover.add_argument("--reason", default="no reason given")

    repair_destination = subparsers.add_parser(
        _admin_name("repair-destination"),
        help="replace only the approved destination after an unrecoverable final movement failure",
    )
    repair_destination.add_argument("submission_id", help=_submission_target_help)
    repair_destination.add_argument("--destination-section-gid", required=True)
    repair_destination.add_argument("--reason", default="no reason given")
    repair_destination.add_argument("--run-id")

    discard = subparsers.add_parser(
        _admin_name("discard"), help="abandon a stale open operation without applying it"
    )
    discard.add_argument("submission_id", help=_submission_target_help)
    discard.add_argument("--reason", default="no reason given")

    abandon = subparsers.add_parser(
        _admin_name("abandon-operation"),
        help=(
            "declare that a prior agent run will not return, retire its exact attempt, and "
            "prepare the safe continuation returned by Dish"
        ),
    )
    abandon.add_argument("submission_id", help=_submission_target_help)
    abandon.add_argument("--reason", default="no reason given")
    abandon.add_argument(
        "--lease-id",
        help="exact actor lease; may be omitted only when one eligible latest attempt exists",
    )

    reconcile_abandonment = subparsers.add_parser(
        _admin_name("reconcile-abandonment"),
        help=(
            "continue an abandonment that Dish could not finish automatically after rereading "
            "the current task and durable evidence"
        ),
    )
    reconcile_abandonment.add_argument(
        "abandonment_id",
        help=(
            "exact abandonment ID, task GID, or supported Asana task URL "
            "(a task GID/URL resolves to that task's active abandonment)"
        ),
    )

    reopen = subparsers.add_parser(
        _admin_name("reopen"),
        help="apply a substantive reset to a held Verification candidate",
    )
    reopen.add_argument("submission_id", help=_submission_target_help)
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

    resolved = subparsers.add_parser(
        _admin_name("resolved"),
        help="release a Verification hold into a fresh Verification round",
    )
    resolved.add_argument("submission_id", help=_submission_target_help)

    migrate = subparsers.add_parser(
        _admin_name("migrate"),
        help="migrate one individually encountered older-schema task after cutover",
    )
    migrate.add_argument("task_gid")

    reopen_planning = subparsers.add_parser(
        _admin_name("reopen-planning"),
        help="explicitly reopen one completed bare task before a new Planning operation",
    )
    reopen_planning.add_argument("task_gid")
    reopen_planning.add_argument("--reason", default="no reason given")
    reopen_planning.add_argument(
        "--request-id",
        help="replay the exact interrupted service request UUID",
    )

    recover_lease = subparsers.add_parser(
        _admin_name("recover-lease"),
        help=(
            "release an expired lease only so the same durable agent run can resume; this never "
            "transfers workflow or Verification-cycle ownership to a fresh run"
        )
    )
    recover_lease.add_argument("submission_id")
    recover_lease.add_argument("--reason", default="no reason given")

    expire_lease = subparsers.add_parser(
        _admin_name("expire-lease"),
        help=(
            "release an active lease when its process is gone; this does not transfer durable "
            "workflow ownership, so rerun dish-admin inspect afterward"
        ),
    )
    expire_lease.add_argument("target")
    expire_lease.add_argument("--reason", default="no reason given")
    expire_lease.add_argument(
        "--request-id",
        help="replay the exact lease-expiry request UUID after an ambiguous response",
    )

    backup_create = subparsers.add_parser(
        _admin_name("backup-create"),
        help="create a validated online snapshot of the shared database",
    )
    backup_create.add_argument("--label", default="manual")

    backup_restore = subparsers.add_parser(
        _admin_name("backup-restore"), help="restore a managed shared-database snapshot"
    )
    backup_restore.add_argument("backup_id")

    authorize_description = (
        "Record Marco's authorization for one exact governed-field before/after change. "
        "This does not edit the task, approve Verification, or submit the dish."
    )
    authorize = subparsers.add_parser(
        _admin_name("authorize-governed-change"),
        help=authorize_description,
        description=authorize_description,
    )
    authorize.add_argument("submission_id", help=_submission_target_help)
    authorize.add_argument("--field", required=True, help="exact governed field named by Dish")
    authorize.add_argument("--before", required=True, type=json.loads, help="typed JSON value before the change")
    authorize.add_argument("--after", required=True, type=json.loads, help="typed JSON value after the change")
    authorize.add_argument(
        "--reason", default="no reason given",
        help="why Marco approves this exact task-scoped change",
    )
    authorize.add_argument("--run-id")

    _hold_help = {
        _admin_name("supply-evidence"): "resume a pending-evidence operation with Marco-supplied evidence",
        _admin_name("record-human-decision"): (
            "record Marco's decision in the task log and release a pending-human-review hold; "
            "does not modify governed fields such as Exemptions or Locks — use "
            "authorize-governed-change for that"
        ),
    }
    _hold_detail_help = {
        _admin_name("supply-evidence"): "the supplied evidence to append to the task record",
        _admin_name("record-human-decision"): (
            "human decision and reasoning to append to Decisions; this text does not itself "
            "change Exemptions, Locks, or other canonical fields"
        ),
    }
    for name, help_text in _hold_help.items():
        hold = subparsers.add_parser(name, help=help_text, description=help_text)
        hold.add_argument("submission_id", help=_submission_target_help)
        hold.add_argument("--detail", required=True, help=_hold_detail_help[name])
        hold.add_argument("--resume-status", required=True, choices=("pending-research", "pending-verification"))
        hold.add_argument("--file", dest="file_path")
        hold.add_argument("--editor", choices=("claude", "gpt", "codex"))
        hold.add_argument("--model")
        hold.add_argument("--run-id")
        hold.add_argument("--expected-task-gid", required=True)
        hold.add_argument("--expected-cycle-id")
        hold.add_argument("--expected-hold-identity")
    return parser


def build_application(profile: str | None = None):
    mode = os.environ.get("DISH_MODE", "").strip().lower()
    client_profile = resolve_client_profile(profile, admin=True)
    service_url = client_profile.service_url
    if mode not in {"", "local", "service"}:
        raise DishRuleError("INVALID_ARGUMENT", "DISH_MODE must be local or service", rule="dish_mode_invalid")
    live_mode = os.environ.get("DISH_LIVE_MODE", "").strip().lower() in {"1", "true", "yes"}
    if not mode and live_mode:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "live mode requires the shared dish service",
            rule="shared_service_required",
        )
    if not mode:
        if service_url:
            mode = "service"
        else:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "DISH_MODE is required; use service for live operation or local only for controlled development",
                rule="dish_mode_required",
            )
    if mode == "service":
        if not service_url:
            raise DishRuleError("INVALID_ARGUMENT", "DISH_SERVICE_URL is required in service mode", rule="service_url_required")
        return DishAdminServiceClient(
            service_url,
            token=client_profile.token,
            run_id=os.environ.get("DISH_CLIENT_RUN_ID", ""),
            connect_timeout=float(os.environ.get("DISH_SERVICE_CLIENT_CONNECT_TIMEOUT", "10")),
            response_timeout=float(os.environ.get("DISH_SERVICE_CLIENT_RESPONSE_TIMEOUT", "600")),
        )
    if live_mode:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "live mode requires the shared dish service",
            rule="shared_service_required",
        )
    lock = DatabaseProcessLock(
        database_process_lock_path(DB_PATH), role="local-admin"
    ).acquire()
    try:
        ServiceDatabaseOwnership(DB_PATH).assert_local_access_allowed()
        honest_root = configured_honest_path()
        app = DishAdminApplication(
            initialize_database(DB_PATH),
            backend=AsanaBackend(),
            release_loader=lambda: resolve_release(honest_root, include_migrations=True),
        )
        app._database_process_lock = lock
        return app
    except Exception:
        lock.release()
        raise


def _build_expire_lease_client(profile: str | None = None) -> DishAdminServiceClient:
    mode = os.environ.get("DISH_MODE", "").strip().lower()
    client_profile = resolve_client_profile(profile, admin=True)
    service_url = client_profile.service_url
    if mode not in {"", "local", "service"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_MODE must be local or service",
            rule="dish_mode_invalid",
        )
    live_mode = os.environ.get("DISH_LIVE_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not mode:
        if service_url:
            mode = "service"
        elif live_mode:
            raise DishRuleError(
                "PROTOCOL_INCOMPATIBLE",
                "live mode requires the shared dish service",
                rule="shared_service_required",
            )
        else:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "DISH_MODE is required; expire-lease requires service mode",
                rule="dish_mode_required",
            )
    if mode != "service":
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "lease expiry requires shared-service mode",
            rule="shared_service_required",
        )
    if not service_url:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_SERVICE_URL is required in service mode",
            rule="service_url_required",
        )
    run_id = require_dish_uuid(
        os.environ.get("DISH_CLIENT_RUN_ID", ""), field="DISH_CLIENT_RUN_ID"
    )
    return DishAdminServiceClient(
        service_url,
        token=client_profile.token,
        run_id=run_id,
        connect_timeout=float(
            os.environ.get("DISH_SERVICE_CLIENT_CONNECT_TIMEOUT", "10")
        ),
        response_timeout=float(
            os.environ.get("DISH_SERVICE_CLIENT_RESPONSE_TIMEOUT", "600")
        ),
    )


def _normalize_output_flags(arguments: Sequence[str]) -> list[str]:
    """Allow global output flags before or after the subcommand."""

    values = list(arguments)
    flags = [flag for flag in ("--json", "--verbose") if flag in values]
    if not flags:
        return values
    values = [token for token in values if token not in flags]
    profile_prefix: list[str] = []
    if "--profile" in values:
        index = values.index("--profile")
        if index + 1 < len(values):
            profile_prefix = values[index : index + 2]
            del values[index : index + 2]
    return [*profile_prefix, *flags, *values]


def _output_profile(arguments: Sequence[str]) -> str:
    return profile_from_argv(arguments) or os.environ.get("DISH_PROFILE", "prod") or "prod"


def _emit_result(result: dict[str, object], *, arguments: Sequence[str]) -> None:
    force_json = "--json" in arguments or not sys.stdout.isatty()
    if force_json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    print(
        render_admin_result(
            result,
            profile=_output_profile(arguments),
            verbose="--verbose" in arguments,
        ),
        end="",
    )


def _normalize_expire_target(target: str) -> tuple[str | None, str | None]:
    clean = str(target or "").strip()
    if clean.isdecimal():
        return None, require_asana_gid(clean, field="task_gid")
    if "://" in clean:
        return None, task_gid_from_url(clean)
    return require_dish_uuid(clean, field="lease_id"), None


def _run_expire_lease(
    arguments: Sequence[str], *, application: object | None
) -> int:
    try:
        parsed = vars(build_parser().parse_args(_normalize_output_flags(arguments)))
        lease_id, task_gid = _normalize_expire_target(parsed["target"])
        reason = parsed["reason"].strip()
        if not reason:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "lease expiry reason is required",
                rule="lease_expiry_reason_required",
                details={"field": "reason"},
            )
        request_id = parsed.get("request_id") or str(uuid.uuid4())
        request_id = require_dish_uuid(request_id, field="request_id")
        requested_profile = parsed.pop("profile", None)
        app = application or (
            _build_expire_lease_client(requested_profile)
            if requested_profile is not None
            else _build_expire_lease_client()
        )
        method = getattr(app, "expire_lease", None)
        if method is None:
            raise DishRuleError(
                "PROTOCOL_INCOMPATIBLE",
                "lease expiry requires shared-service mode",
                rule="shared_service_required",
            )
        run_id = require_dish_uuid(
            getattr(app, "run_id", None), field="DISH_CLIENT_RUN_ID"
        )
        print(
            f"expire-lease request_id={request_id} run_id={run_id}",
            file=sys.stderr,
            flush=True,
        )
        result = method(
            lease_id=lease_id,
            task_gid=task_gid,
            reason=reason,
            request_id=request_id,
        )
    except DishRuleError as exc:
        result = error_envelope(
            "expire-lease",
            exc,
            task_gid=locals().get("task_gid"),
        )
    except Exception as exc:
        result = error_envelope(
            "expire-lease",
            DishRuleError(
                "INTERNAL_ERROR",
                "dish-admin command failed",
                rule="command_failure",
                details={"error_type": type(exc).__name__},
            ),
            task_gid=locals().get("task_gid"),
        )
    _emit_result(result, arguments=arguments)
    return exit_status(result["code"])


def _argument_context(argv: Sequence[str]) -> dict[str, str | None]:
    argv = argv_without_profile(argv)
    command_index = next(
        (index for index, token in enumerate(argv) if token in _ADMIN_COMMANDS),
        None,
    )
    command = "unknown" if command_index is None else argv[command_index]
    command_argv = [] if command_index is None else argv[command_index:]
    submission_id = None
    task_gid = None
    if (
        command in _OPERATION_ADMIN_COMMANDS
        and len(command_argv) > 1
        and not command_argv[1].startswith("-")
    ):
        submission_id = command_argv[1]
    if command in {"migrate", "reopen-planning"} and len(command_argv) > 1 and not command_argv[1].startswith("-"):
        task_gid = command_argv[1]
    return {"command": command, "submission_id": submission_id, "task_gid": task_gid}


def main(
    argv: Sequence[str] | None = None,
    *,
    application: DishAdminApplication | None = None,
) -> int:
    arguments = _normalize_output_flags(sys.argv[1:] if argv is None else argv)
    command_arguments = argv_without_profile(arguments)

    if "-h" in arguments or "--help" in arguments:
        build_parser().parse_args(arguments)  # prints help and raises SystemExit(0)

    if command_arguments and command_arguments[0] == "expire-lease":
        return _run_expire_lease(arguments, application=application)

    context = _argument_context(arguments)
    owned_application = application is None
    try:
        requested_profile = profile_from_argv(arguments)
        app = application or (
            build_application(requested_profile)
            if requested_profile is not None
            else build_application()
        )
    except DishRuleError as exc:
        result = error_envelope(
            context["command"] or "unknown",
            exc,
            task_gid=context["task_gid"],
            submission_id=context["submission_id"],
        )
        _emit_result(result, arguments=arguments)
        return exit_status(result["code"])
    except Exception as exc:
        error = DishRuleError(
            "INTERNAL_ERROR",
            "dish-admin failed during startup",
            rule="startup_failure",
            details={"error_type": type(exc).__name__},
        )
        result = error_envelope(context["command"] or "unknown", error)
        _emit_result(result, arguments=arguments)
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
            parsed.pop("profile", None)
            parsed.pop("json", None)
            parsed.pop("verbose", None)
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
                request_id = parsed.pop("request_id", None)
                if request_id is not None:
                    if not isinstance(app, DishAdminServiceClient):
                        result = error_envelope(
                            command,
                            DishRuleError(
                                "PROTOCOL_INCOMPATIBLE",
                                "exact request replay requires shared-service mode",
                                rule="shared_service_required",
                            ),
                            task_gid=parsed.get("task_gid"),
                        )
                    else:
                        result = app.execute(
                            command, parsed, request_id=request_id
                        )
                else:
                    result = app.execute(command, **parsed)
    except DishRuleError as exc:
        result = error_envelope(
            context["command"] or "unknown",
            exc,
            task_gid=context["task_gid"],
            submission_id=context["submission_id"],
        )
    except Exception as exc:
        error = DishRuleError(
            "INTERNAL_ERROR",
            "dish-admin command failed",
            rule="command_failure",
            details={"error_type": type(exc).__name__},
        )
        result = error_envelope(
            context["command"] or "unknown",
            error,
            task_gid=context["task_gid"],
            submission_id=context["submission_id"],
        )
    try:
        _emit_result(result, arguments=arguments)
        return exit_status(result["code"])
    finally:
        if owned_application:
            backend = getattr(app, "backend", None)
            close = getattr(backend, "close", None)
            if callable(close):
                close()
            conn = getattr(app, "conn", None)
            if conn is not None:
                conn.close()
            process_lock = getattr(app, "_database_process_lock", None)
            if process_lock is not None:
                process_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
