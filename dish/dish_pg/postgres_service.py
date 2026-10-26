"""PostgreSQL authority adapter for the existing Dish HTTP service.

This module deliberately reuses ``dish-service`` and ``dish_service.http``. It
does not introduce another listener, routing table, authentication model, or
command framework. TEST rehearsal and a future cutover runtime therefore share
this service composition; environment/startup policy decides where it may run.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from alembic.runtime.migration import MigrationContext
from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import (
    DBAPIError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as PoolTimeoutError,
)

from dish_service.leases import ServicePrincipal
from dish_tool.admin_command_spec import RESOLVED_OPERATION_TARGET_COMMANDS
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope, result_envelope

from . import models
from . import stage3_models as wf
from .command_contract import (
    ACTION_COMMANDS,
    ADMIN_COMMANDS,
    COMMAND_DEFINITIONS,
    COOKED_UPDATES_COMMAND,
    QUERY_COMMAND,
    SEARCH_COMMAND,
    SEARCH_PAGE_SIZE_DEFAULT,
    normalize_postgres_search_arguments,
    validate_postgres_action_request,
)
from .command_port import CommandCall, CommandPortError, CommandResult, PostgresCommandPort
from .command_port_common import task_reference_from_dish
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .frontend_board_query import FrontendBoardQuery
from .openapi import postgres_action_openapi
from .read_model import InvalidCursor, PostgresReadModel, ReadModelError
from .stage_context import stage_context_for_start
from .workflow import (
    RequestIdentityConflict,
    WorkflowAuthorityError,
    WorkflowAuthorityService,
)


LOGGER = logging.getLogger("dish.pg.service")


def database_failure_error(
    exc: SQLAlchemyError, *, outcome: str = "", request_id: str | None = None
) -> DishRuleError:
    """Classify a database-layer failure without reporting every one as an outage.

    Only connectivity-class failures mean PostgreSQL authority is unavailable and
    a retry can help. Any other database error (constraint, schema, data or
    statement failure) is a deterministic service fault: retrying will not help
    and calling it an outage sends operators after the wrong problem. The
    original exception is always logged because the response carries only the
    error type.
    """
    unavailable = isinstance(
        exc, (OperationalError, InterfaceError, PoolTimeoutError)
    ) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)
    error_type = type(exc).__name__
    rule = (
        "postgresql_authority_unavailable"
        if unavailable
        else "postgresql_database_error"
    )
    LOGGER.error(
        "postgresql_database_failure rule=%s error_type=%s request_id=%s outcome=%s",
        rule,
        error_type,
        request_id,
        outcome,
        exc_info=exc,
    )
    suffix = f"; {outcome}" if outcome else ""
    if unavailable:
        return DishRuleError(
            "BACKEND_REJECTED",
            f"PostgreSQL authority is unavailable{suffix}",
            rule=rule,
            retryable=True,
            details={"error_type": error_type},
        )
    return DishRuleError(
        "BACKEND_REJECTED",
        f"PostgreSQL is reachable but the database rejected the operation "
        f"({error_type}){suffix}",
        rule=rule,
        retryable=False,
        details={"error_type": error_type},
    )


def workflow_authority_error(exc: WorkflowAuthorityError) -> DishRuleError:
    """Preserve actionable run-identity failures without weakening other conflicts."""

    message = str(exc)
    if message.startswith(
        (
            "automatic run creation is limited",
            "run registration or bootstrap authority is required",
        )
    ):
        return DishRuleError(
            "CONFLICT",
            message,
            rule="postgresql_run_registration_required",
            details={"required_next_action": "obtain_registered_client_run"},
        )
    if message.startswith("run is stale, retired"):
        return DishRuleError(
            "CONFLICT",
            message,
            rule="postgresql_run_identity_stale",
            details={
                "required_next_action": "use_fresh_client_run",
                "fresh_request_id_required": True,
            },
        )
    return DishRuleError(
        "CONFLICT",
        message,
        rule="postgresql_command_rejected",
    )


def _dish_admin_prefix(profile: str) -> list[str]:
    """Return ``dish-admin`` plus ``--profile`` whenever the profile is not prod.

    A bare ``dish-admin`` defaults to prod, so a printed command must name any
    other profile or copying it would act on production.
    """

    clean = str(profile or "prod").strip().lower() or "prod"
    return ["dish-admin"] if clean == "prod" else ["dish-admin", "--profile", clean]


def _admin_action_from_queue(
    action: Mapping[str, Any], *, profile: str
) -> dict[str, Any] | None:
    """Render one queue action as the exact ``dish-admin`` command for inspect.

    The command carries every identifier the queue already derived, so the
    operator never has to reconstruct operation, cycle, or hold identifiers.
    Only Marco-supplied text remains a placeholder.
    """

    kind = str(action.get("kind") or "")
    operation_id = str(action.get("operation_id") or "").strip()
    if not operation_id:
        return None
    options: list[dict[str, Any]] = []
    requires_input: list[str] = []
    if kind == "supply_evidence":
        command = "supply-evidence"
        summary = "Supply the evidence Dish is waiting for."
        options.append({"flag": "--detail", "value": "<evidence>"})
        requires_input.append("detail")
        if action.get("cycle_id"):
            options.append({"flag": "--expected-cycle-id", "value": str(action["cycle_id"])})
        if action.get("hold_identity"):
            options.append(
                {"flag": "--expected-hold-identity", "value": str(action["hold_identity"])}
            )
        effect = (
            "record the evidence and resume the Dish at "
            f"{action.get('resume_status') or 'its derived status'}"
        )
    elif kind == "resolve_verification_hold":
        command = "resolved"
        summary = "Release the Verification hold."
        effect = "release the Verification hold into a fresh Verification round"
    elif kind == "reopen_verification_stop":
        command = "reopen"
        summary = "Reopen Verification only after a substantive reset."
        for flag, value, key in (
            ("--category", "<evidence|premise|method|scope>", "category"),
            ("--before", "<exact old text>", "before"),
            ("--after", "<exact new text>", "after"),
            ("--editor", "<claude|gpt|codex>", "editor"),
            ("--model", "<model>", "model"),
            ("--run-id", "<run-id>", "run_id"),
            ("--file", "<corrected-task-file>", "file_path"),
            ("--date", "<YYYY-MM-DD>", "date"),
        ):
            options.append({"flag": flag, "value": value})
            requires_input.append(key)
        effect = (
            "install the corrected candidate and open a fresh Verification round only after "
            "evidence, premise, method, or scope changed concretely"
        )
    elif kind == "semantic_proposal":
        # Proposal approval is a per-change ceremony owned by the queue.
        return {
            "kind": "queue",
            "summary": "Review the proposed governed change in the queue.",
            "effect": "review the exact proposed changes and authorize or reject them",
            "shell_command": " ".join([*_dish_admin_prefix(profile), "queue"]),
            "arguments": {"positional": [], "options": []},
        }
    else:
        return None
    parts = [*_dish_admin_prefix(profile), command, operation_id]
    for option in options:
        parts.extend([option["flag"], shlex.quote(str(option["value"]))])
    rendered: dict[str, Any] = {
        "kind": command,
        "queue_kind": kind,
        "summary": summary,
        "effect": effect,
        "shell_command": " ".join(parts),
        "arguments": {"positional": [operation_id], "options": options},
    }
    if requires_input:
        rendered["requires_input"] = requires_input
    return rendered


def _admin_inspect_next_actions(
    queue: Mapping[str, Any], *, profile: str
) -> dict[str, Any]:
    """Project the single-Dish queue derivation into admin inspect fields."""

    items = queue.get("issue_items") if isinstance(queue.get("issue_items"), list) else []
    signals = [
        signal
        for item in items
        if isinstance(item, Mapping)
        for signal in (item.get("signals") or ())
        if isinstance(signal, Mapping)
    ]
    actions: list[dict[str, Any]] = []
    for signal in signals:
        action = signal.get("queue_action")
        if isinstance(action, Mapping):
            rendered = _admin_action_from_queue(action, profile=profile)
            if rendered is not None:
                actions.append(rendered)
    fields: dict[str, Any] = {"queue_signals": [dict(signal) for signal in signals]}
    if actions:
        fields["human_actions"] = actions
    blocking = next(
        (
            signal
            for signal in signals
            if signal.get("category") in {"needs_marco", "unsafe"}
        ),
        signals[0] if signals else None,
    )
    if blocking is not None:
        fields["problem"] = str(blocking.get("summary") or "")
        if blocking.get("kind") == "evidence_hold" and blocking.get("detail"):
            fields["hold_question"] = str(blocking["detail"])
    return fields


_SUPPORTED_PROFILES = ("test", "prod")
_SEARCH_CURSOR_KIND = "active-title-search-v1"
_SEARCH_PROJECTION_DELAY = timedelta(seconds=1)

# Retained admin-principal commands are exposed only through the private admin
# transport; every other retained command remains reachable from the agent
# surface. A small number of agent commands retain private-admin compatibility
# while their normal user interaction happens directly in the connected agent.
# Retired/non-retained commands stay unroutable on both surfaces.
_ADMIN_EXPOSED_COMMANDS = frozenset(
    name
    for name in ADMIN_COMMANDS
    if COMMAND_DEFINITIONS[name].retained
)
# `hold-reject` opens an Evidence hold. Dish no longer accepts new Evidence
# holds from any caller; the command survives only for the shadow worker's
# in-process translation of legacy SQLite `reject` records, which calls the
# command port directly rather than any HTTP surface.
_INTERNAL_ONLY_COMMANDS = frozenset({"hold-reject"})
_AGENT_EXPOSED_COMMANDS = frozenset(
    name
    for name, definition in COMMAND_DEFINITIONS.items()
    if definition.retained
    and definition.principal not in {"admin", "historical"}
    and name not in _INTERNAL_ONLY_COMMANDS
)


_SECTION4_CONTROL_POINTS_FIRED: set[tuple[str, str]] = set()


def _section4_control_point(
    *, point: str, request_id: uuid.UUID | None, command: str
) -> None:
    """Reach an explicit Section 4 barrier in PostgreSQL TEST runtime only.

    Fires at most once per (point, request_id) per process: a retried request
    reusing the same request_id (idempotent-replay scenarios) must not hit an
    already-torn-down barrier on its second pass through this code path.
    """

    configured = os.environ.get("DISH_SECTION4_SERVICE_CONTROL_POINT", "").strip()
    if configured != point or request_id is None:
        return
    expected_request = os.environ.get("DISH_SECTION4_SERVICE_REQUEST_ID", "").strip()
    if expected_request != str(request_id):
        return
    fired_key = (point, str(request_id))
    if fired_key in _SECTION4_CONTROL_POINTS_FIRED:
        return
    _SECTION4_CONTROL_POINTS_FIRED.add(fired_key)
    socket_path = os.environ.get("DISH_SECTION4_SERVICE_BARRIER_SOCKET", "").strip()
    if not socket_path:
        raise RuntimeError("Section 4 service control point omitted its barrier socket")
    label = (
        "service_after_execute_before_commit"
        if point == "after_execute_before_commit"
        else "service_after_commit_before_response"
    )
    message = {
        "schema": "dish-section4-barrier-event-v1",
        "label": label,
        "pid": os.getpid(),
        "payload": {"command": command, "command_request_id": str(request_id)},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(
            json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError(f"Section 4 barrier {label!r} closed without release")
            received.extend(chunk)
    response = json.loads(received.decode("utf-8"))
    expected = {
        "schema": "dish-section4-barrier-event-v1",
        "action": "continue",
        "label": label,
    }
    if response != expected:
        raise RuntimeError(f"Section 4 barrier {label!r} returned invalid release")


class PostgresRuntimeService:
    """Expose PostgreSQL command authority through the established HTTP service."""

    _SUPPORTED_HTTP_SURFACES = frozenset({"agent", "action"})
    _profile: str = "test"

    def __init__(
        self,
        config,
        *,
        database_url: str,
        cursor_secret: bytes,
        expected_database: str,
        expected_schema_head: str,
        expected_release: str,
        expected_generation_id: uuid.UUID,
        profile: str = "test",
    ) -> None:
        if profile not in _SUPPORTED_PROFILES:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "PostgreSQL runtime profile must be test or prod",
                rule="postgresql_runtime_profile_invalid",
                details={"profile": profile},
            )
        self.config = config
        self._engine = create_database_engine(DatabaseSettings(url=database_url))
        self._session_maker = session_factory(self._engine)
        self._cursor_secret = cursor_secret
        self._expected_database = expected_database
        self._expected_schema_head = expected_schema_head
        self._expected_release = expected_release
        self._expected_generation_id = expected_generation_id
        self._profile = profile

    def close(self) -> None:
        self._engine.dispose()

    def supports_http_route(self, surface: str, command: str) -> bool:
        """Expose only routes implemented by the PostgreSQL authority adapter.

        The shared transport owns more private/admin routes than this adapter.
        The public Action surface is intentionally limited to the already
        implemented PostgreSQL command contract; unsupported legacy Action
        commands remain hidden rather than falling through to another backend.
        Admin-principal commands are reachable only through the admin
        transport, never the agent route. PROD retains the complete supported
        admin inventory; TEST exposes only the non-recovery queue, archive, and unarchive
        commands needed to qualify the operator surface.
        """

        if surface == "agent":
            return command in _AGENT_EXPOSED_COMMANDS
        if surface == "action":
            return command in ACTION_COMMANDS
        if surface in {"admin", "admin-lease", "admin-lease-expiry"}:
            return command in _ADMIN_EXPOSED_COMMANDS
        return False

    def action_openapi(self, *, server_url: str) -> dict[str, Any]:
        """Return the PostgreSQL Action contract served by the shared listener."""

        return postgres_action_openapi(server_url=server_url)

    def validate_action_request(
        self, command: str, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate the PostgreSQL-specific no-Asana Action contract."""

        return validate_postgres_action_request(command, request)

    @staticmethod
    def action_error_envelope(
        command: str, error: DishRuleError, *, http_status: int
    ) -> dict[str, Any]:
        """Project pre-dispatch Action failures into the public PostgreSQL shape."""

        payload = error_envelope(command, error)
        payload["http_status"] = http_status
        payload["data"]["request_replayed"] = False
        return payload

    def _identity(self) -> dict[str, Any]:
        try:
            with session_scope(self._session_maker) as session:
                database_name = str(session.scalar(text("SELECT current_database()")))
                current_heads = tuple(
                    sorted(MigrationContext.configure(session.connection()).get_current_heads())
                )
                if database_name != self._expected_database:
                    raise DishRuleError(
                        "BACKEND_REJECTED",
                        "PostgreSQL runtime database identity does not match the rehearsal binding",
                        rule="postgresql_runtime_identity_mismatch",
                        retryable=False,
                        details={
                            "expected_database": self._expected_database,
                            "observed_database": database_name,
                        },
                    )
                if current_heads != (self._expected_schema_head,):
                    raise DishRuleError(
                        "BACKEND_REJECTED",
                        "PostgreSQL runtime schema does not match the exact expected Alembic head",
                        rule="postgresql_runtime_schema_mismatch",
                        retryable=False,
                        details={
                            "expected_schema_head": self._expected_schema_head,
                            "observed_schema_heads": list(current_heads),
                        },
                    )
                generation = session.scalar(
                    select(models.AuthorityGeneration).where(
                        models.AuthorityGeneration.status == "active"
                    )
                )
                if generation is None:
                    raise DishRuleError(
                        "BACKEND_REJECTED",
                        "PostgreSQL authority has no active generation",
                        rule="postgresql_generation_missing",
                        retryable=False,
                    )
        except DishRuleError:
            raise
        except SQLAlchemyError as exc:
            raise database_failure_error(exc) from exc

        observed = {
            "database": database_name,
            "schema_head": current_heads[0],
            "dish_release": generation.dish_release,
            "generation_id": str(generation.generation_id),
            "generation_status": generation.status,
        }
        expected = {
            "database": self._expected_database,
            "schema_head": self._expected_schema_head,
            "dish_release": self._expected_release,
            "generation_id": str(self._expected_generation_id),
            "generation_status": "active",
        }
        if observed != expected:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL runtime identity does not match the rehearsal binding",
                rule="postgresql_runtime_identity_mismatch",
                retryable=False,
                details={"expected": expected, "observed": observed},
            )
        return observed

    def startup_check(self) -> dict[str, Any]:
        identity = self._identity()
        return {
            "ok": True,
            "startup_ready": True,
            "backend": "postgresql",
            "profile": self._profile,
            "pid": os.getpid(),
            "identity": identity,
            "isolation": {
                "asana_environment_keys": sorted(
                    key for key, value in os.environ.items() if "ASANA" in key.upper() and value
                ),
                "bind_host": self.config.bind_host,
                "action_bind_host": self.config.action_bind_host,
                "legacy_writer_fence_path": None,
                "supported_http_surfaces": sorted(self._SUPPORTED_HTTP_SURFACES),
            },
        }

    def health(self) -> dict[str, Any]:
        try:
            return self.startup_check()
        except DishRuleError as exc:
            return {
                "ok": False,
                "startup_ready": False,
                "backend": "postgresql",
                "profile": self._profile,
                "pid": os.getpid(),
                "code": exc.code,
                "rule": exc.rule,
                "retryable": exc.retryable,
            }

    def record_replay_validation_failure(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str,
        error: DishRuleError,
    ) -> dict[str, Any]:
        """Persist a pre-execution failure through canonical PostgreSQL command authority."""

        try:
            run_id = uuid.UUID(principal.run_id)
            parsed_request_id = uuid.UUID(request_id)
        except ValueError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service command identifiers are invalid",
                rule="service_identifier_invalid",
            ) from exc

        definition = COMMAND_DEFINITIONS.get(command)
        if definition is None or definition.principal not in {"agent", "admin"}:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "command is not eligible for replay-bound HTTP validation",
                rule="postgresql_validation_principal_invalid",
                details={"command": command},
            )

        envelope = error_envelope(command, error)
        envelope["data"]["request_id"] = request_id
        recorded_at = datetime.now(timezone.utc)
        try:
            with session_scope(self._session_maker) as session:
                authoritative, replayed = PostgresCommandPort(
                    session,
                    cursor_secret=self._cursor_secret,
                ).record_validation_failure(
                    CommandCall(
                        command_name=command,
                        arguments=dict(arguments),
                        owner_id=principal.owner_id,
                        principal_class=definition.principal,
                        run_id=run_id,
                        request_id=parsed_request_id,
                        now=recorded_at,
                    ),
                    result_payload=envelope,
                    invocation_surface="postgresql-http-validation",
                )
                session.flush()
            if replayed:
                authoritative.setdefault("data", {})["request_replayed"] = True
                authoritative["data"]["request_id"] = request_id
            return authoritative
        except RequestIdentityConflict as exc:
            raise DishRuleError(
                "CONFLICT",
                "request ID was already used for different work",
                rule="service_request_identity_conflict",
                details={"request_id": request_id},
            ) from exc
        except CommandPortError as exc:
            raise DishRuleError(
                "CONFLICT",
                str(exc),
                rule="postgresql_command_rejected",
            ) from exc
        except WorkflowAuthorityError as exc:
            raise workflow_authority_error(exc) from exc
        except SQLAlchemyError as exc:
            raise database_failure_error(
                exc, outcome="validation failure was not recorded", request_id=request_id
            ) from exc


    def renew_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Bridge the shared lease route into the canonical PostgreSQL command."""

        return self.execute_agent(
            "renew-lease",
            {"operation_id": operation_id},
            principal=principal,
            request_id=request_id,
        )

    def _active_lease_id_for_operation(self, operation_id: str) -> str:
        with session_scope(self._session_maker) as session:
            lease = session.scalar(
                select(wf.ServiceLease).where(
                    wf.ServiceLease.operation_id == uuid.UUID(str(operation_id)),
                    wf.ServiceLease.state == "active",
                )
            )
            if lease is None:
                raise DishRuleError(
                    "CONFLICT",
                    "no matching active lease for this operation",
                    rule="service_lease_not_found",
                    details={"operation_id": str(operation_id)},
                )
            return str(lease.lease_id)

    def _lease_and_task_for_lease_id(self, lease_id: str) -> tuple[str, str]:
        with session_scope(self._session_maker) as session:
            lease = session.get(wf.ServiceLease, uuid.UUID(str(lease_id)))
            if lease is None:
                raise DishRuleError(
                    "NOT_FOUND",
                    "unknown lease",
                    rule="service_lease_not_found",
                    details={"lease_id": str(lease_id)},
                )
            return str(lease.lease_id), str(lease.task_id)

    def _lease_and_task_for_task_gid(self, task_gid: str) -> tuple[str, str]:
        with session_scope(self._session_maker) as session:
            task = session.scalar(
                select(models.DishTask)
                .join(
                    models.TaskExternalAlias,
                    models.TaskExternalAlias.task_id == models.DishTask.task_id,
                )
                .where(
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.external_id == str(task_gid),
                    models.TaskExternalAlias.state == "active",
                )
            )
            if task is None:
                raise DishRuleError(
                    "NOT_FOUND",
                    "unknown active Dish task",
                    rule="service_task_not_found",
                    details={"task_gid": str(task_gid)},
                )
            lease = session.scalar(
                select(wf.ServiceLease).where(
                    wf.ServiceLease.task_id == task.task_id,
                    wf.ServiceLease.state == "active",
                )
            )
            if lease is None:
                raise DishRuleError(
                    "CONFLICT",
                    "no matching active lease for this task",
                    rule="service_lease_not_found",
                    details={"task_gid": str(task_gid)},
                )
            return str(lease.lease_id), str(task.task_id)

    def recover_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Bridge the private admin lease-recovery route onto ``recover-lease``.

        Operation/lease identity is resolved exclusively from PostgreSQL
        ``ServiceLease``; no Asana lookup is performed.
        """

        lease_id = self._active_lease_id_for_operation(operation_id)
        return self.execute_admin(
            "recover-lease",
            {"operation_id": str(operation_id), "lease_id": lease_id, "reason": reason},
            principal=principal,
            request_id=request_id,
        )

    def expire_lease(
        self,
        principal: ServicePrincipal,
        *,
        lease_id: str | None = None,
        task_gid: str | None = None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Bridge the private admin lease-expiry route onto ``expire-lease``.

        Task/lease identity is resolved exclusively from PostgreSQL
        (``ServiceLease`` and the local ``TaskExternalAlias`` compatibility
        mapping); no Asana lookup is performed.
        """

        if lease_id is not None:
            resolved_lease_id, resolved_task_id = self._lease_and_task_for_lease_id(lease_id)
        else:
            if task_gid is None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "exactly one of lease_id and task_gid is required",
                    rule="lease_expiry_target_invalid",
                )
            resolved_lease_id, resolved_task_id = self._lease_and_task_for_task_gid(task_gid)
        return self.execute_admin(
            "expire-lease",
            {"lease_id": resolved_lease_id, "task_id": resolved_task_id, "reason": reason},
            principal=principal,
            request_id=request_id,
        )

    def _execute_search(
        self,
        session,
        arguments: Mapping[str, Any],
    ) -> CommandResult:
        query = str(arguments["query"])
        page_size = int(arguments["page_size"])
        scope = str(arguments.get("scope", "active"))
        reads = PostgresReadModel(session, cursor_secret=self._cursor_secret)
        try:
            native_result = reads.native_search(
                query=query, page_size=page_size, cursor=arguments.get("cursor"), scope=scope
            )
        except InvalidCursor as exc:
            return CommandResult(False, SEARCH_COMMAND, "INVALID_ARGUMENT", 400, {"message": str(exc), "field": "cursor"})
        except ReadModelError as exc:
            return CommandResult(False, SEARCH_COMMAND, "BACKEND_REJECTED", 409, {"message": str(exc)}, retryable=True)
        if native_result is not None:
            return CommandResult(True, SEARCH_COMMAND, "OK", 200, native_result)
        board = FrontendBoardQuery(session)
        context = board.context()
        normalized_query = query.lower()
        offset = 0
        cursor = arguments.get("cursor")
        if cursor is not None:
            try:
                payload = reads.cursor_codec.decode(str(cursor))
            except InvalidCursor as exc:
                return CommandResult(
                    False,
                    SEARCH_COMMAND,
                    "INVALID_ARGUMENT",
                    400,
                    {"message": str(exc), "field": "cursor"},
                )
            expected = {
                "kind": _SEARCH_CURSOR_KIND,
                "generation_id": str(context.generation_id),
                "registry_version_id": str(context.registry_version_id),
                "registry_revision": context.registry_revision,
                "query": normalized_query,
                "page_size": page_size,
            }
            if payload.get("scope", "active") != scope or any(
                payload.get(key) != value for key, value in expected.items()
            ):
                return CommandResult(
                    False,
                    SEARCH_COMMAND,
                    "INVALID_ARGUMENT",
                    400,
                    {"message": "cursor is stale or belongs to another search query", "field": "cursor"},
                )
            try:
                offset = int(payload["offset"])
            except (KeyError, TypeError, ValueError):
                return CommandResult(
                    False,
                    SEARCH_COMMAND,
                    "INVALID_ARGUMENT",
                    400,
                    {"message": "cursor page boundary is invalid", "field": "cursor"},
                )
            if offset < 0:
                return CommandResult(
                    False,
                    SEARCH_COMMAND,
                    "INVALID_ARGUMENT",
                    400,
                    {"message": "cursor page boundary is invalid", "field": "cursor"},
                )

        # Active Search uses the frontend board primitive. All-status Search reads the
        # same generation's current Dish state without active-board eligibility. Both
        # paths bind pagination to the captured authority context.
        results: list[dict[str, Any]] = []
        if scope == "all":
            rows = session.execute(
                select(models.DishTask.task_id, models.ContentVersion.title,
                       models.DishState.section_id, models.SectionRegistryEntry.display_name,
                       models.SectionRegistryEntry.workflow_role, models.GovernedProject.logical_name,
                       models.DishState.completed, models.DishState.completion_reason,
                       models.DishState.archived_at)
                .select_from(models.DishTask)
                .join(models.DishState, and_(
                    models.DishState.task_id == models.DishTask.task_id,
                    models.DishState.generation_id == context.generation_id))
                .join(models.ContentVersion, and_(
                    models.ContentVersion.task_id == models.DishTask.task_id,
                    models.ContentVersion.content_version_id == models.DishState.current_content_version_id))
                .outerjoin(models.SectionRegistryEntry, and_(
                    models.SectionRegistryEntry.registry_version_id == models.DishState.registry_version_id,
                    models.SectionRegistryEntry.section_id == models.DishState.section_id))
                .outerjoin(models.GovernedSection,
                           models.GovernedSection.section_id == models.DishState.section_id)
                .outerjoin(models.GovernedProject,
                           models.GovernedProject.project_id == models.GovernedSection.project_id)
                .where(models.DishTask.existence_state.in_(("ordinary", "isolated")),
                       func.lower(models.ContentVersion.title).contains(normalized_query, autoescape=True))
                .order_by(func.lower(models.ContentVersion.title), models.DishTask.task_id)
                .offset(offset).limit(page_size + 1)
            ).all()
            has_more = len(rows) > page_size
            visible = rows[:page_size]
        else:
            facts = board.search_titles(
                query=query, projection_delay=_SEARCH_PROJECTION_DELAY,
                max_results=offset + page_size + 1, context=context,
            )
            visible = facts.results[offset : offset + page_size]
            has_more = facts.truncated or len(facts.results) > offset + page_size
        for fact in visible:
            task_id = fact.task_id
            task_gid = session.scalar(
                select(models.TaskExternalAlias.external_id).where(
                    models.TaskExternalAlias.task_id == task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            result = {
                "dish_id": str(task_id),
                "title": fact.title,
                "section_id": str(fact.section_id),
                "section_label": fact.display_name if scope == "all" else fact.section_label,
                "workflow_role": fact.workflow_role,
                "project_label": fact.logical_name if scope == "all" else fact.project_label,
                "status": ("archived" if fact.archived_at is not None else
                           "active" if not fact.completed else
                           "cooked" if fact.completion_reason == "cooked" else "completed")
                          if scope == "all" else "active",
            }
            if task_gid is not None:
                result["task_gid"] = str(task_gid)
            results.append(result)

        current_context = board.context()
        current_identity = (
            current_context.generation_id,
            current_context.registry_version_id,
            current_context.registry_revision,
        )
        captured_identity = (
            context.generation_id,
            context.registry_version_id,
            context.registry_revision,
        )
        if current_identity != captured_identity:
            return CommandResult(
                False,
                SEARCH_COMMAND,
                "BACKEND_REJECTED",
                409,
                {
                    "message": "Search authority context changed during the read; retry from the first page",
                    "captured_generation_id": str(context.generation_id),
                    "captured_registry_version_id": str(context.registry_version_id),
                    "captured_registry_revision": context.registry_revision,
                },
                retryable=True,
            )

        next_cursor = None
        if has_more:
            next_cursor = reads.cursor_codec.encode(
                {
                    "kind": _SEARCH_CURSOR_KIND,
                    "generation_id": str(context.generation_id),
                    "registry_version_id": str(context.registry_version_id),
                    "registry_revision": context.registry_revision,
                    "query": normalized_query,
                    "page_size": page_size,
                    **({"scope": scope} if scope == "all" else {}),
                    "offset": offset + page_size,
                }
            )
        return CommandResult(
            True,
            SEARCH_COMMAND,
            "OK",
            200,
            {
                "query": query,
                "scope": scope,
                "results": results,
                "next_cursor": next_cursor,
                "page_size": page_size,
                "generation_id": str(context.generation_id),
                "registry_version_id": str(context.registry_version_id),
                "registry_revision": context.registry_revision,
            },
        )

    @staticmethod
    def _bootstrap_agent(
        *, principal_class: str, arguments: Mapping[str, Any]
    ) -> str | None:
        if principal_class == "admin":
            return "marco"
        raw_agent = arguments.get("agent")
        if not isinstance(raw_agent, str):
            return None
        agent = raw_agent.strip()
        return agent if agent in {"claude", "gpt", "codex"} else None

    def _execute_command(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None,
        request_id: str | None,
        principal_class: str,
    ) -> dict[str, Any]:
        if principal is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service principal is required",
                rule="service_principal_required",
            )
        try:
            run_id = uuid.UUID(principal.run_id)
            parsed_request_id = uuid.UUID(request_id) if request_id is not None else None
        except ValueError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service command identifiers are invalid",
                rule="service_identifier_invalid",
            ) from exc

        try:
            with session_scope(self._session_maker) as session:
                execution_command = COOKED_UPDATES_COMMAND if command == QUERY_COMMAND else command
                if command == SEARCH_COMMAND:
                    if parsed_request_id is not None:
                        result = CommandResult(
                            False,
                            SEARCH_COMMAND,
                            "INVALID_ARGUMENT",
                            400,
                            {"message": "read-only Search does not accept request_id"},
                        )
                    else:
                        try:
                            search_arguments = normalize_postgres_search_arguments(arguments)
                        except DishRuleError as exc:
                            result = CommandResult(
                                False,
                                SEARCH_COMMAND,
                                exc.code,
                                400,
                                {"message": str(exc), **dict(exc.details)},
                            )
                        else:
                            result = self._execute_search(session, search_arguments)
                else:
                    definition = COMMAND_DEFINITIONS[execution_command]
                    if definition.retained and definition.profile != "Q":
                        generation_id = session.scalar(
                            select(models.AuthorityGeneration.generation_id).where(
                                models.AuthorityGeneration.status == "active"
                            )
                        )
                        if generation_id is None:
                            raise WorkflowAuthorityError("no active authority generation")
                        existing_request = (
                            session.get(wf.ServiceRequest, parsed_request_id)
                            if parsed_request_id is not None
                            else None
                        )
                        if existing_request is None:
                            WorkflowAuthorityService(session).ensure_initial_cutover_run(
                                generation_id=generation_id,
                                run_id=run_id,
                                owner_id=principal.owner_id,
                                agent=self._bootstrap_agent(
                                    principal_class=principal_class, arguments=arguments
                                ),
                                registered_at=datetime.now(timezone.utc),
                            )
                    result = PostgresCommandPort(
                        session,
                        cursor_secret=self._cursor_secret,
                    ).execute(
                        CommandCall(
                            command_name=execution_command,
                            arguments=dict(arguments),
                            owner_id=principal.owner_id,
                            principal_class=principal_class,
                            run_id=run_id,
                            request_id=parsed_request_id,
                            now=datetime.now(timezone.utc),
                        )
                    )
                    if command == QUERY_COMMAND:
                        result = replace(result, command=QUERY_COMMAND)
                session.flush()
                _section4_control_point(
                    point="after_execute_before_commit",
                    request_id=parsed_request_id,
                    command=command,
                )
            _section4_control_point(
                point="after_commit_before_response",
                request_id=parsed_request_id,
                command=command,
            )
            payload = asdict(result)
            data = dict(payload.pop("data"))
            data["request_replayed"] = payload.pop("request_replayed")
            if command == "start" and payload.get("ok"):
                stage_context = stage_context_for_start(
                    kind=arguments.get("kind"),
                    honest_root=self.config.honest_root,
                )
                if stage_context is not None:
                    data["stage_context"] = stage_context
            payload["data"] = data
            if principal.owner_id == self.config.action_client_id or (
                principal_class == "admin" and command in {"archive", "unarchive"}
            ):
                # Guidance is attached before JSON encoding, so normalize the
                # immutable tuple fields to their public JSON array shape now.
                # The private archive CLI consumes the same canonical command
                # contract even though other native private clients retain the
                # transitional compact PostgreSQL response family.
                payload["allowed_actions"] = list(payload["allowed_actions"])
                payload["errors"] = list(payload["errors"])
            else:
                # The native CLI/private client still consumes the established
                # PostgreSQL service family. The public Action principal receives
                # the richer canonical workflow envelope.
                for field in (
                    "task_gid",
                    "submission_id",
                    "state",
                    "allowed_actions",
                    "errors",
                ):
                    payload.pop(field, None)
            return payload
        except CommandPortError as exc:
            raise DishRuleError(
                "CONFLICT",
                str(exc),
                rule="postgresql_command_rejected",
            ) from exc
        except WorkflowAuthorityError as exc:
            raise workflow_authority_error(exc) from exc
        except SQLAlchemyError as exc:
            raise database_failure_error(
                exc, outcome="governed mutation was not admitted", request_id=request_id
            ) from exc

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if command in _INTERNAL_ONLY_COMMANDS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "command is not exposed to the PostgreSQL agent surface",
                rule="agent_command_forbidden",
            )
        return self._execute_command(
            command,
            arguments,
            principal=principal,
            request_id=request_id,
            principal_class="agent",
        )

    def execute_admin(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if command not in _ADMIN_EXPOSED_COMMANDS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "command is not exposed to the PostgreSQL admin surface",
                rule="admin_command_forbidden",
            )
        if command == "inspect":
            reference = task_reference_from_dish(str(arguments.get("dish") or ""))
            if reference is None:
                return error_envelope(
                    command,
                    DishRuleError(
                        "INVALID_ARGUMENT", "Dish reference is required",
                        rule="dish_target_required",
                    ),
                )
            try:
                with session_scope(self._session_maker) as session:
                    reads = PostgresReadModel(
                        session, cursor_secret=self._cursor_secret
                    )
                    view = reads.task_view(reference)
                    task = session.get(models.DishTask, view.task_id)
                    recovery = (
                        None
                        if task is None
                        else PostgresCommandPort(
                            session, cursor_secret=self._cursor_secret
                        ).legacy_verification_recovery(
                            generation_id=reads.active_generation().generation_id,
                            task=task,
                        )
                    )
            except ReadModelError as exc:
                return error_envelope(
                    command,
                    DishRuleError(
                        "NOT_FOUND", str(exc), rule="admin_dish_target_not_found"
                    ),
                )
            next_actions = self._admin_inspect_queue_fields(view.task_id)
            active_lease_id = (
                None
                if view.operation_id is None
                else self._admin_inspect_active_lease_id(view.operation_id)
            )
            return result_envelope(
                command=command,
                state=view.completion_state,
                data={
                    "dish_id": str(view.task_id),
                    "task_title": view.title,
                    "status": view.completion_state,
                    "completion_state": view.completion_state,
                    "completed": view.completed,
                    "operation_id": (
                        None if view.operation_id is None else str(view.operation_id)
                    ),
                    "operation_phase": view.operation_phase,
                    "active_lease_id": active_lease_id,
                    **(
                        {
                            "workflow_recovery": recovery,
                            "required_admin_action": recovery["command"],
                        }
                        if recovery is not None
                        and recovery["command"] == "adopt-legacy-verification"
                        else {}
                    ),
                    **next_actions,
                },
            )
        if command == "archive" and arguments.get("confirmed") is not True:
            reference = task_reference_from_dish(str(arguments.get("dish") or ""))
            if reference is None:
                return error_envelope(
                    command,
                    DishRuleError(
                        "INVALID_ARGUMENT", "Dish reference is required",
                        rule="dish_target_required",
                    ),
                )
            try:
                with session_scope(self._session_maker) as session:
                    view = PostgresReadModel(
                        session, cursor_secret=self._cursor_secret
                    ).task_view(reference)
            except ReadModelError as exc:
                return error_envelope(
                    command,
                    DishRuleError(
                        "NOT_FOUND", str(exc), rule="admin_dish_target_not_found"
                    ),
                )
            if view.completion_state == "archived":
                return result_envelope(
                    command=command,
                    state="archived",
                    data={
                        "dish_id": str(view.task_id),
                        "task_title": view.title,
                        "completion_state": "archived",
                        "already_archived": True,
                        "request_id": request_id,
                    },
                )
            if view.completed:
                result = error_envelope(
                    command,
                    DishRuleError(
                        "TASK_NOT_ACTIVE",
                        "Archive requires an incomplete active Dish",
                        rule="archive_task_not_active",
                    ),
                )
                result["data"].update({
                    "dish_id": str(view.task_id),
                    "task_title": view.title,
                })
                return result
            result = error_envelope(
                command,
                DishRuleError(
                    "CONFIRMATION_REQUIRED",
                    "Archive confirmation is required; no task mutation was performed.",
                    rule="archive_confirmation_required",
                ),
            )
            result["data"].update({
                "dish_id": str(view.task_id),
                "task_title": view.title,
                "request_id": request_id,
                "confirmation_prompt": (
                    f"Archive \u201c{view.title}\u201d ({view.task_id})? It will leave "
                    "active/search views; all history will be preserved. [y/N]"
                ),
            })
            return result
        resolution: dict[str, str] | None = None
        if command in RESOLVED_OPERATION_TARGET_COMMANDS and str(
            arguments.get("submission_id") or ""
        ).strip():
            target = str(arguments["submission_id"]).strip()
            try:
                operation_id, dish_id = self._resolve_admin_operation_target_source(target)
            except DishRuleError as exc:
                return error_envelope(command, exc)
            arguments = {**arguments, "submission_id": operation_id}
            if dish_id is not None:
                resolution = {
                    "target": target,
                    "dish_id": dish_id,
                    "operation_id": operation_id,
                }
        result = self._execute_command(
            command,
            arguments,
            principal=principal,
            request_id=(
                request_id if COMMAND_DEFINITIONS[command].request_replay else None
            ),
            principal_class="admin",
        )
        if resolution is not None:
            # Show the operator exactly which operation a Dish target acted on.
            data = result.get("data")
            if not isinstance(data, dict):
                data = {}
                result["data"] = data
            data.setdefault("operation_id", resolution["operation_id"])
            data["resolved_operation_target"] = resolution
        return result

    def _admin_inspect_active_lease_id(self, operation_id: Any) -> str | None:
        """Return the operation's active actor lease ID, the exact pin abandon-operation needs."""

        with session_scope(self._session_maker) as session:
            lease_id = session.scalar(
                select(wf.ServiceLease.lease_id).where(
                    wf.ServiceLease.operation_id == uuid.UUID(str(operation_id)),
                    wf.ServiceLease.lease_kind == "actor",
                    wf.ServiceLease.state == "active",
                )
            )
        return None if lease_id is None else str(lease_id)

    def _admin_inspect_queue_fields(self, task_id: uuid.UUID) -> dict[str, Any]:
        """Derive inspect's next actions without letting a queue failure hide the hold.

        The queue derivation runs in its own session so a failure there cannot
        poison the already-read hold state; inspect then reports a warning.
        """

        try:
            with session_scope(self._session_maker) as session:
                queue = PostgresCommandPort(
                    session, cursor_secret=self._cursor_secret
                )._queue(task_id=task_id)
            return _admin_inspect_next_actions(queue, profile=self._profile)
        except Exception as exc:  # noqa: BLE001 - inspect must still show the hold
            LOGGER.exception("admin inspect queue derivation failed for %s", task_id)
            return {
                "warnings": [
                    "Next actions are unavailable because the queue derivation "
                    f"failed ({type(exc).__name__}); the hold state shown is current."
                ]
            }

    def _resolve_admin_operation_target(self, raw: str) -> str:
        """Resolve an admin operation target to one exact open operation ID.

        An exact operation ID passes through unchanged. A Dish UUID, Asana task
        GID/URL, or frontend /dishes/<uuid>/... URL resolves to that Dish's
        single open workflow operation; zero or several is a clear refusal.
        """

        return self._resolve_admin_operation_target_source(raw)[0]

    def _resolve_admin_operation_target_source(self, raw: str) -> tuple[str, str | None]:
        """Return ``(operation_id, dish_id)``; ``dish_id`` is set only when resolved from a Dish."""

        clean = raw.strip()
        try:
            parsed = uuid.UUID(clean)
        except ValueError:
            parsed = None
        with session_scope(self._session_maker) as session:
            if parsed is not None and session.get(wf.WorkflowOperation, parsed) is not None:
                return clean, None
            reference = task_reference_from_dish(clean)
            if reference is None:
                return clean, None
            reads = PostgresReadModel(session, cursor_secret=self._cursor_secret)
            try:
                task = reads.resolve_task(reference)
            except ReadModelError as exc:
                raise DishRuleError(
                    "NOT_FOUND",
                    "no workflow operation or Dish matches that target",
                    rule="admin_operation_target_not_found",
                    details={"target": clean},
                ) from exc
            generation = reads.active_generation()
            operation_ids = list(
                session.scalars(
                    select(wf.WorkflowOperation.operation_id)
                    .where(
                        wf.WorkflowOperation.generation_id == generation.generation_id,
                        wf.WorkflowOperation.task_id == task.task_id,
                        wf.WorkflowOperation.lifecycle == "open",
                    )
                    .order_by(
                        wf.WorkflowOperation.created_at,
                        wf.WorkflowOperation.operation_id,
                    )
                )
            )
        if not operation_ids:
            raise DishRuleError(
                "NOT_FOUND",
                "that Dish has no open workflow operation; run "
                f"{' '.join([*_dish_admin_prefix(self._profile), 'inspect'])} for its state",
                rule="admin_operation_target_not_found",
                details={"dish_id": str(task.task_id)},
            )
        if len(operation_ids) > 1:
            raise DishRuleError(
                "CONFLICT",
                "that Dish has several open workflow operations; pass the exact operation ID",
                rule="admin_operation_target_ambiguous",
                details={
                    "dish_id": str(task.task_id),
                    "operation_ids": [str(value) for value in operation_ids],
                },
            )
        return str(operation_ids[0]), str(task.task_id)
