"""Human-first argparse surface for the environment-scoped ``dish-admin`` executable."""

from __future__ import annotations

import argparse
import json
import logging
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
from dish_tool.database_initialization import initialize_database
from dish_tool.backend import AsanaBackend
from dish_tool.admin_command_spec import (
    ADMIN_COMMANDS as _ADMIN_COMMANDS,
    ADMIN_COMMAND_SPECS,
    RESOLVED_OPERATION_TARGET_COMMANDS as _OPERATION_ADMIN_COMMANDS,
)
from dish_tool.admin_human import render_admin_result
from dish_tool.errors import DishRuleError
from dish_tool.identifiers import require_asana_gid, require_dish_uuid
from dish_tool.releases import configured_honest_path, resolve_release
from dish_tool.results import error_envelope, exit_status

LOG = logging.getLogger("dish.admin_cli")
from dish_tool.task_urls import task_gid_from_url
from dish_service.client import DishAdminServiceClient
from dish_service.database_ownership import ServiceDatabaseOwnership, database_process_lock_path
from dish_service.process_lock import DatabaseProcessLock



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
            "`dish-admin inspect <dish>`; use low-level mutation commands only when "
            "Dish returns them as the safe next action. Agents may use this tool only against "
            "the test profile; production administration remains Marco-only."
        ),
        epilog=(
            "Normal use: inspect one Dish; queue processes work waiting for Marco; audit checks "
            "fleet integrity; active shows current run ownership; kill safely replaces one run. "
            "Advanced recovery, review-detail, migration, backup, and governance commands remain "
            "callable when Dish returns one as an exact next action, but are intentionally omitted "
            "from this top-level help. No admin command submits a dish."
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
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    _submission_target_help = (
        "exact operation ID, Dish/task GID, supported Asana task URL, Dish UUID, or "
        "frontend /dishes/<uuid>/<decorative-title-slug> URL"
    )
    _dish_target_help = (
        "Dish GID, Dish UUID, supported Asana task URL, or frontend "
        "/dishes/<uuid>/<decorative-title-slug> URL; the UUID is authoritative and "
        "the decorative slug is ignored"
    )

    inspect_admin = subparsers.add_parser(
        _admin_name("inspect"),
        help="inspect one Dish and show its exact current state and safe next action",
        description=(
            "Inspect one Dish using its canonical identity. This is the exact drill-down for "
            "workflow state, recovery legality, and safe continuation guidance."
        ),
    )
    inspect_admin.add_argument("dish", metavar="DISH", help=_dish_target_help)

    archive = subparsers.add_parser(
        _admin_name("archive"),
        help="archive one resting Dish while preserving its history",
        description="archive one resting Dish without deleting or rewriting its history",
    )
    archive.add_argument("dish", metavar="DISH", help=_dish_target_help)
    archive.add_argument(
        "--request-id",
        help="replay the exact interrupted archive request UUID",
    )
    archive.add_argument(
        "--yes", dest="confirmed", action="store_true",
        help="confirm archive without an interactive prompt",
    )

    queue = subparsers.add_parser(
        _admin_name("queue"),
        help="work through everything currently waiting for Marco",
        description=(
            "Show the dishes that currently require Marco and, in an interactive terminal, "
            "process Human Review, evidence, proposal, or recovery work directly."
        ),
    )
    queue.add_argument(
        "--non-interactive",
        action="store_true",
        help="print the queue and exit even in an interactive terminal",
    )

    issues = subparsers.add_parser(
        _admin_name("issues"),
        help=argparse.SUPPRESS,
        description="Deprecated compatibility alias for `dish-admin queue`.",
    )
    issues.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)

    attention = subparsers.add_parser(
        _admin_name("attention"),
        help=argparse.SUPPRESS,
        description="Deprecated compatibility alias for `dish-admin queue`.",
    )
    attention.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser(
        _admin_name("audit"),
        help="audit Dish population integrity without treating Marco-managed Asana organization as drift",
        description=(
            "Read-only confidence audit of configured Asana Cooking tasks versus Dish-known "
            "records. Healthy and expected/manual rows are hidden unless --verbose is used."
        ),
    )

    subparsers.add_parser(
        _admin_name("active"),
        help="show current Dish run ownership and whether each lease is active or expired",
        description=(
            "Read-only authority diagnostic. Internal workflow stage and exact lease/run identifiers "
            "are shown only with --verbose."
        ),
    )
    subparsers.add_parser(
        _admin_name("active-leases"),
        help=argparse.SUPPRESS,
        description="Deprecated compatibility alias for `dish-admin active`.",
    )

    kill = subparsers.add_parser(
        _admin_name("kill"),
        help="safely revoke one agent run and prepare its continuation",
        description=(
            "Revoke the exact outstanding run. Dish preserves confirmed work/checkpoints, "
            "reconciles uncertain effects where possible, and refuses rather than interrupt an "
            "in-progress committed mutation. This does not terminate an external ChatGPT process."
        ),
    )
    kill.add_argument("dish", metavar="DISH", help=_dish_target_help)
    kill.add_argument(
        "--reason",
        default="Marco requested replacement of the outstanding Dish run.",
        help="durable reason recorded for the fencing/replacement decision",
    )

    kill_all_expired = subparsers.add_parser(
        _admin_name("kill-all-expired"),
        help="safely replace every run whose actor lease has expired",
        description=(
            "Cautious bulk form of dish-admin kill. Only exact runs with expired unreleased "
            "actor leases are selected; each kill remains independently fenced."
        ),
    )
    kill_all_expired.add_argument(
        "--reason",
        default="Marco requested replacement of every run whose actor lease has expired.",
    )
    kill_all_expired.add_argument(
        "--yes", dest="confirmed", action="store_true",
        help="confirm the bulk kill without an interactive prompt",
    )

    kill_all = subparsers.add_parser(
        _admin_name("kill-all"),
        help="safely replace every run that currently holds an actor lease",
        description=(
            "Blunt development/dark-launch bulk form of dish-admin kill. Each exact run is "
            "fenced independently; this command is not all-or-nothing."
        ),
    )
    kill_all.add_argument(
        "--reason",
        default="Marco confirmed that no currently leased Dish run should remain authorized.",
    )
    kill_all.add_argument(
        "--yes", dest="confirmed", action="store_true",
        help="confirm the bulk kill without an interactive prompt",
    )

    # Compatibility/detail review commands remain callable but are not normal navigation.
    review_queue = subparsers.add_parser(
        _admin_name("review-queue"),
        help=argparse.SUPPRESS,
        description="Compatibility/detail view for durable Human Review and semantic proposals.",
    )
    review_queue.add_argument(
        "--status", choices=("active", "pending", "approved", "all"), default="active"
    )
    review_queue.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)

    review_inspect = subparsers.add_parser(_admin_name("review-inspect"), help=argparse.SUPPRESS)
    review_inspect.add_argument("proposal_id")

    review_approve = subparsers.add_parser(_admin_name("review-approve"), help=argparse.SUPPRESS)
    review_approve.add_argument("proposal_id")
    review_approve.add_argument("--reason")
    review_approve.add_argument(
        "--choice",
        help="for Human Review, choose A-F or 'other'; A is the agent's recommended route",
    )
    review_approve.add_argument(
        "--detail",
        help="for Human Review items, Marco's complete decision and reasoning",
    )

    review_reject = subparsers.add_parser(_admin_name("review-reject"), help=argparse.SUPPRESS)
    review_reject.add_argument("proposal_id")
    review_reject.add_argument("--reason", required=True)

    subparsers.add_parser(_admin_name("holds"), help=argparse.SUPPRESS)

    recover = subparsers.add_parser(
        _admin_name("recover"),
        help=argparse.SUPPRESS,
        description=(
            "reconcile an uncertain external write or movement from a fresh live reread; "
            "this is not lease recovery"
        ),
    )
    recover.add_argument("submission_id", help=_submission_target_help)
    recover.add_argument(
        "--outcome",
        default="inspect",
        choices=("inspect", "not-applied", "applied"),
        help=(
            "default: inspect and reconcile only what fresh live evidence proves; "
            "applied/not-applied are advanced manual assertions and fail closed on contradiction"
        ),
    )
    recover.add_argument("--reason", default="no reason given")

    repair_destination = subparsers.add_parser(
        _admin_name("repair-destination"),
        help=argparse.SUPPRESS,
        description="replace only the approved destination after an unrecoverable final movement failure",
    )
    repair_destination.add_argument("submission_id", help=_submission_target_help)
    repair_destination.add_argument("--destination-section-gid", required=True)
    repair_destination.add_argument("--reason", default="no reason given")
    repair_destination.add_argument("--run-id")

    discard = subparsers.add_parser(
        _admin_name("discard"), help=argparse.SUPPRESS, description="abandon a stale open operation without applying it"
    )
    discard.add_argument("submission_id", help=_submission_target_help)
    discard.add_argument("--reason", default="no reason given")

    abandon = subparsers.add_parser(
        _admin_name("abandon-operation"),
        help=argparse.SUPPRESS,
        description=(
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
        help=argparse.SUPPRESS,
        description=(
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
        help=argparse.SUPPRESS,
        description="apply a substantive reset to a held Verification candidate",
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
        help=argparse.SUPPRESS,
        description="release a Verification hold into a fresh Verification round",
    )
    resolved.add_argument("submission_id", help=_submission_target_help)

    migrate = subparsers.add_parser(
        _admin_name("migrate"),
        help=argparse.SUPPRESS,
        description="migrate one individually encountered older-schema task after cutover",
    )
    migrate.add_argument("task_gid")

    reopen_planning = subparsers.add_parser(
        _admin_name("reopen-planning"),
        help=argparse.SUPPRESS,
        description="explicitly reopen one completed bare task before a new Planning operation",
    )
    reopen_planning.add_argument("task_gid")
    reopen_planning.add_argument("--reason", default="no reason given")
    reopen_planning.add_argument(
        "--request-id",
        help="replay the exact interrupted service request UUID",
    )

    recover_lease = subparsers.add_parser(
        _admin_name("recover-lease"),
        help=argparse.SUPPRESS,
        description=(
            "release an expired lease only so the same durable agent run can resume; this never "
            "transfers workflow or Verification-cycle ownership to a fresh run"
        )
    )
    recover_lease.add_argument("submission_id")
    recover_lease.add_argument("--reason", default="no reason given")

    expire_lease = subparsers.add_parser(
        _admin_name("expire-lease"),
        help=argparse.SUPPRESS,
        description=(
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
        help=argparse.SUPPRESS,
        description="create a validated online snapshot of the shared database",
    )
    backup_create.add_argument("--label", default="manual")

    backup_restore = subparsers.add_parser(
        _admin_name("backup-restore"), help=argparse.SUPPRESS, description="restore a managed shared-database snapshot"
    )
    backup_restore.add_argument("backup_id")

    authorize_description = (
        "Record Marco's authorization for one exact governed-field before/after change. "
        "This does not edit the task, approve Verification, or submit the dish."
    )
    authorize = subparsers.add_parser(
        _admin_name("authorize-governed-change"),
        help=argparse.SUPPRESS,
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
        hold = subparsers.add_parser(name, help=argparse.SUPPRESS, description=help_text)
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

    # Keep compatibility/escape-hatch commands callable without presenting them as
    # normal operator choices in the root help. argparse does not natively hide
    # subparser choices, so remove only their pseudo-actions from help rendering;
    # the actual parser map remains intact.
    subparsers._choices_actions = [
        action
        for action in subparsers._choices_actions
        if action.help != argparse.SUPPRESS
    ]
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


def _emit_interactive_admin_result(
    result: dict[str, object], *, arguments: Sequence[str]
) -> None:
    print(
        render_admin_result(
            result,
            profile=_output_profile(arguments),
            verbose="--verbose" in arguments,
            interactive=True,
        ),
        end="",
    )


def _prompt_review(input_fn, prompt: str) -> str:
    try:
        return str(input_fn(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return "q"


def _human_action_of_kind(result: dict[str, object], kind: str) -> dict[str, object] | None:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    candidates: list[object] = []
    action = data.get("human_action")
    if isinstance(action, dict):
        candidates.append(action)
    actions = data.get("human_actions")
    if isinstance(actions, list):
        candidates.extend(actions)
    for candidate in candidates:
        if isinstance(candidate, dict) and str(candidate.get("kind") or "") == kind:
            return candidate
    return None


def _interactive_supply_evidence(
    app,
    *,
    target: str,
    arguments: Sequence[str],
    input_fn,
    question: str | None = None,
) -> bool:
    """Resolve one evidence queue item directly. Return True when the user chose Quit."""

    inspected = app.execute("inspect", dish=target, verbose="--verbose" in arguments)
    if not inspected.get("ok"):
        print()
        _emit_interactive_admin_result(inspected, arguments=arguments)
        return False
    action = _human_action_of_kind(inspected, "supply-evidence")
    if action is None:
        print()
        _emit_interactive_admin_result(inspected, arguments=arguments)
        return False
    summary = str(action.get("summary") or "Provide the missing evidence.").strip()
    inspect_data = inspected.get("data") if isinstance(inspected.get("data"), dict) else {}
    resolved_question = str(
        question or inspect_data.get("hold_question") or ""
    ).strip()
    print(f"\n{summary}")
    if resolved_question:
        print(f"Question: {resolved_question}")
    detail = _prompt_review(input_fn, "Answer: ")
    if detail.lower() in {"q", "quit", "exit"}:
        return True
    if not detail:
        print("No evidence recorded.\n")
        return False
    structured = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
    positional = structured.get("positional") if isinstance(structured.get("positional"), list) else []
    options = structured.get("options") if isinstance(structured.get("options"), list) else []
    if not positional:
        print("Dish did not provide a complete evidence action; inspect the Dish for details.\n")
        return False
    kwargs: dict[str, object] = {
        "submission_id": str(positional[0]),
        "detail": detail,
    }
    for option in options:
        if not isinstance(option, dict):
            continue
        flag = str(option.get("flag") or "").strip()
        value = option.get("value")
        if not flag.startswith("--") or value is None:
            continue
        if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
            continue
        kwargs[flag[2:].replace("-", "_")] = value
    result = app.execute("supply-evidence", **kwargs)
    print()
    _emit_interactive_admin_result(result, arguments=arguments)
    print()
    return False


def _interactive_issues(
    app,
    *,
    arguments: Sequence[str],
    input_fn=None,
) -> int:
    """Primary Marco queue; compatibility name retained for internal callers/tests."""

    if input_fn is None:
        input_fn = input

    while True:
        result = app.execute("queue")
        _emit_interactive_admin_result(result, arguments=arguments)
        if not result.get("ok"):
            return exit_status(str(result.get("code") or "INTERNAL_ERROR"))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = data.get("issue_items") if isinstance(data.get("issue_items"), list) else data.get("attention_items") if isinstance(data.get("attention_items"), list) else []
        visible = (
            rows
            if "--verbose" in arguments
            else [row for row in rows if isinstance(row, dict) and bool(row.get("needs_you"))]
        )
        if not visible:
            return 0

        choice = _prompt_review(input_fn, "\nQueue number (q to quit): ").lower()
        if choice in {"q", "quit", "exit"}:
            return 0
        if not choice.isdecimal() or not (1 <= int(choice) <= len(visible)):
            print(f"Choose a number from 1 to {len(visible)}, or q to quit.\n")
            continue
        selected = visible[int(choice) - 1]
        if not isinstance(selected, dict):
            print("That queue row is not actionable.\n")
            continue
        target = str(selected.get("dish_id") or selected.get("task_gid") or "").strip()
        signals = selected.get("signals") if isinstance(selected.get("signals"), list) else []
        review_id = next(
            (
                str(signal.get("review_id") or "").strip()
                for signal in signals
                if isinstance(signal, dict) and str(signal.get("review_id") or "").strip()
            ),
            "",
        )
        if review_id:
            if _interactive_review_item(
                app, review_id=review_id, arguments=arguments, input_fn=input_fn
            ):
                return 0
            continue
        if str(selected.get("queue_group") or "") == "evidence" and target:
            evidence_question = next(
                (
                    str(signal.get("detail") or "").strip()
                    for signal in signals
                    if isinstance(signal, dict)
                    and str(signal.get("kind") or "") == "evidence_hold"
                    and str(signal.get("detail") or "").strip()
                ),
                "",
            )
            if _interactive_supply_evidence(
                app,
                target=target,
                arguments=arguments,
                input_fn=input_fn,
                question=evidence_question or None,
            ):
                return 0
            continue
        if not target:
            print("That queue row has no canonical Dish identity.\n")
            continue
        inspected = app.execute("inspect", dish=target, verbose="--verbose" in arguments)
        print()
        _emit_interactive_admin_result(inspected, arguments=arguments)
        if not inspected.get("ok"):
            return exit_status(str(inspected.get("code") or "INTERNAL_ERROR"))
        answer = _prompt_review(input_fn, "\n[0] Back to queue  [Q] Quit: ").lower()
        if answer in {"q", "quit", "exit"}:
            return 0
        print()



def _interactive_attention(app, *, arguments: Sequence[str], input_fn=None) -> int:
    """Compatibility wrapper for the renamed interactive issues flow."""
    return _interactive_issues(app, arguments=arguments, input_fn=input_fn)

def _interactive_review_item(
    app,
    *,
    review_id: str,
    arguments: Sequence[str],
    input_fn,
) -> bool:
    """Process one durable review item. Return True when the user chose Quit."""

    while True:
        inspected = app.execute("review-inspect", proposal_id=review_id)
        print()
        _emit_interactive_admin_result(inspected, arguments=arguments)
        if not inspected.get("ok"):
            return False
        inspect_data = inspected.get("data") if isinstance(inspected.get("data"), dict) else {}
        item = inspect_data.get("review_item") if isinstance(inspect_data.get("review_item"), dict) else {}
        item_type = str(item.get("item_type") or "semantic_proposal")
        item_status = str(item.get("status") or "")

        if item_status != "pending":
            answer = _prompt_review(input_fn, "\n[0] Back  [Q] Quit: ").lower()
            return answer in {"q", "quit", "exit"}

        if item_type == "human_review":
            options = item.get("human_review_options") if isinstance(item.get("human_review_options"), list) else []
            option_ids = [
                str(option.get("option_id") or "").lower()
                for option in options if isinstance(option, dict)
            ]
            option_prompt = "/".join(option_id.upper() for option_id in option_ids)
            prefix = f"Choose {option_prompt}, " if option_prompt else ""
            action = _prompt_review(
                input_fn,
                f"\n{prefix}[O] Other (type an instruction)  [0] Back  [Q] Quit: ",
            ).lower()
            if action in {"q", "quit", "exit"}:
                return True
            if action in {"0", "back", ""}:
                return False
            if action in option_ids:
                result = app.execute(
                    "review-approve", proposal_id=review_id, choice=action.upper()
                )
            elif action in {"o", "other"}:
                decision = _prompt_review(input_fn, "Instruction for the next agent: ")
                if decision.lower() in {"q", "quit", "exit"}:
                    return True
                if not decision:
                    print("No decision recorded.\n")
                    continue
                result = app.execute(
                    "review-approve", proposal_id=review_id, choice="other", reason=decision
                )
            else:
                allowed = ", ".join(option_id.upper() for option_id in option_ids)
                prefix = f"Choose {allowed}, O, 0, or Q." if allowed else "Choose O, 0, or Q."
                print(prefix + "\n")
                continue
        elif item_type == "verification_hold":
            action = _prompt_review(
                input_fn, "\n[R] Release hold to fresh Verification  [0] Back  [Q] Quit: "
            ).lower()
            if action in {"q", "quit", "exit"}:
                return True
            if action in {"0", "back", ""}:
                return False
            if action != "r":
                print("Choose R, 0, or Q.\n")
                continue
            result = app.execute("review-approve", proposal_id=review_id)
        else:
            action = _prompt_review(
                input_fn,
                "\n[A] Approve exact shown bundle  [R] Reject proposal  [0] Back  [Q] Quit: ",
            ).lower()
            if action in {"q", "quit", "exit"}:
                return True
            if action in {"0", "back", ""}:
                return False
            if action == "a":
                result = app.execute(
                    "review-approve",
                    proposal_id=review_id,
                    reason="Approved interactively after reviewing the exact linked change bundle.",
                )
            elif action == "r":
                reason = _prompt_review(input_fn, "Reason for rejection: ")
                if reason.lower() in {"q", "quit", "exit"}:
                    return True
                if not reason:
                    print("No rejection recorded.\n")
                    continue
                result = app.execute("review-reject", proposal_id=review_id, reason=reason)
            else:
                print("Choose A, R, 0, or Q.\n")
                continue

        print()
        _emit_interactive_admin_result(result, arguments=arguments)
        if not result.get("ok"):
            return False
        print()
        return False


def _interactive_review_queue(
    app,
    *,
    status: str,
    arguments: Sequence[str],
    input_fn=None,
) -> int:
    """Compatibility/detail review queue; the primary operator flow is ``queue``."""

    if input_fn is None:
        input_fn = input

    while True:
        queue = app.execute("review-queue", status=status)
        _emit_interactive_admin_result(queue, arguments=arguments)
        if not queue.get("ok"):
            return exit_status(str(queue.get("code") or "INTERNAL_ERROR"))
        data = queue.get("data") if isinstance(queue.get("data"), dict) else {}
        items = data.get("review_items") if isinstance(data.get("review_items"), list) else []
        if not items:
            return 0

        choice = _prompt_review(input_fn, "\nReview number (q to quit): ").lower()
        if choice in {"q", "quit", "exit"}:
            return 0
        if not choice.isdecimal() or not (1 <= int(choice) <= len(items)):
            print(f"Choose a number from 1 to {len(items)}, or q to quit.\n")
            continue
        selected = items[int(choice) - 1]
        if not isinstance(selected, dict):
            print("That queue row is not reviewable.\n")
            continue
        review_id = str(selected.get("review_id") or selected.get("proposal_id") or "").strip()
        if not review_id:
            print("That queue row has no durable review ID.\n")
            continue
        if _interactive_review_item(
            app, review_id=review_id, arguments=arguments, input_fn=input_fn
        ):
            return 0

def _attach_recovery_continuation(
    app, result: dict, *, submission_id: str
) -> dict:
    """Attach one fresh inspect after a successful CLI recovery action."""
    if not result.get("ok"):
        return result
    execute = getattr(app, "execute", None)
    if execute is None:
        return result
    continuation = execute("inspect", submission_id=submission_id)
    data = result.setdefault("data", {})
    if not continuation.get("ok"):
        data["post_recovery_error"] = {
            "code": continuation.get("code"),
            "errors": continuation.get("errors")
            if isinstance(continuation.get("errors"), list)
            else [],
        }
        result["allowed_actions"] = []
        return result
    continuation_data = continuation.get("data")
    data["post_recovery"] = (
        dict(continuation_data) if isinstance(continuation_data, dict) else {}
    )
    result["allowed_actions"] = list(continuation.get("allowed_actions") or [])
    if continuation.get("state") is not None:
        result["state"] = continuation.get("state")
    return result


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
        LOG.error(
            "dish_admin_startup_failure command=%s error_type=%s",
            context["command"] or "unknown",
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
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
            verbose_requested = bool(parsed.pop("verbose", False))
            non_interactive = bool(parsed.pop("non_interactive", False))
            if command == "inspect":
                parsed["verbose"] = verbose_requested
            interactive_terminal = (
                not non_interactive
                and "--json" not in arguments
                and sys.stdin.isatty()
                and sys.stdout.isatty()
            )
            archive_preflight_complete = False
            if command == "archive" and not parsed.get("confirmed") and interactive_terminal:
                preflight = app.execute(
                    "archive", dish=parsed["dish"], confirmed=False
                )
                if preflight.get("ok") or preflight.get("code") != "CONFIRMATION_REQUIRED":
                    result = preflight
                    archive_preflight_complete = True
                else:
                    preflight_data = (
                        preflight.get("data")
                        if isinstance(preflight.get("data"), dict)
                        else {}
                    )
                    prompt = str(preflight_data.get("confirmation_prompt") or "").strip()
                    answer = _prompt_review(input, prompt).lower()
                    if answer not in {"y", "yes"}:
                        print("Archive cancelled; no Dish was changed.")
                        return 0
                    parsed["confirmed"] = True
            if command in {"kill-all", "kill-all-expired"} and not parsed.get("confirmed") and interactive_terminal:
                scope = "all currently leased Dish runs" if command == "kill-all" else "all Dish runs with expired leases"
                answer = _prompt_review(
                    input,
                    f"This will permanently revoke {scope}. Continue? [y/N]: ",
                ).lower()
                if answer not in {"y", "yes"}:
                    print("No Dish runs were killed.")
                    return 0
                parsed["confirmed"] = True
            if archive_preflight_complete:
                pass
            elif command in {"queue", "issues", "attention"} and interactive_terminal:
                interactive_exit = _interactive_issues(app, arguments=arguments)
                result = None
            elif command == "review-queue" and interactive_terminal:
                interactive_exit = _interactive_review_queue(
                    app, status=parsed.get("status", "active"), arguments=arguments
                )
                result = None
            elif command == "recover-lease":
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
                    result = _attach_recovery_continuation(
                        app, result, submission_id=parsed["submission_id"]
                    )
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
        if result is None:
            return interactive_exit
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
