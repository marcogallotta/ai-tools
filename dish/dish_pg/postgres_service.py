"""PostgreSQL authority adapter for the existing Dish HTTP service.

This module deliberately reuses ``dish-service`` and ``dish_service.http``. It
does not introduce another listener, routing table, authentication model, or
command framework. TEST rehearsal and a future cutover runtime therefore share
this service composition; environment/startup policy decides where it may run.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from alembic.runtime.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope, result_envelope

from . import models
from . import stage3_models as wf
from .command_contract import (
    ACTION_COMMANDS,
    ADMIN_COMMANDS,
    COMMAND_DEFINITIONS,
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
from .workflow import RequestIdentityConflict, WorkflowAuthorityError


_SUPPORTED_PROFILES = ("test", "prod")
_SEARCH_CURSOR_KIND = "active-title-search-v1"
_SEARCH_PROJECTION_DELAY = timedelta(seconds=1)

# Retained admin-principal commands are exposed only through the private admin
# transport; every other retained command remains reachable from the agent
# surface. Retired/non-retained commands (e.g. historical backup-create/
# backup-restore) stay unroutable on both surfaces.
_ADMIN_EXPOSED_COMMANDS = frozenset(
    name
    for name in ADMIN_COMMANDS
    if COMMAND_DEFINITIONS[name].retained
)
_AGENT_EXPOSED_COMMANDS = frozenset(
    name
    for name, definition in COMMAND_DEFINITIONS.items()
    if definition.retained and definition.principal not in {"admin", "historical"}
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
        transport, never the agent route, and only when this runtime is bound
        to the PROD profile: TEST rehearsals never expose live recovery
        authority, matching TEST evidence never proving PROD.
        """

        if surface == "agent":
            return command in _AGENT_EXPOSED_COMMANDS
        if surface == "action":
            return command in ACTION_COMMANDS
        if surface in {"admin", "admin-lease", "admin-lease-expiry"}:
            return self._profile == "prod" and command in _ADMIN_EXPOSED_COMMANDS
        return False

    def action_openapi(self, *, server_url: str) -> dict[str, Any]:
        """Return the PostgreSQL Action contract served by the shared listener."""

        return postgres_action_openapi(server_url=server_url)

    def validate_action_request(
        self, command: str, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate the PostgreSQL-specific no-Asana Action contract."""

        return validate_postgres_action_request(command, request)

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
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL authority is unavailable",
                rule="postgresql_authority_unavailable",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc

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
        except (CommandPortError, WorkflowAuthorityError) as exc:
            raise DishRuleError(
                "CONFLICT",
                str(exc),
                rule="postgresql_command_rejected",
            ) from exc
        except SQLAlchemyError as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL authority is unavailable; validation failure was not recorded",
                rule="postgresql_authority_unavailable",
                retryable=True,
                details={"error_type": type(exc).__name__},
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
        reads = PostgresReadModel(session, cursor_secret=self._cursor_secret)
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
            if any(payload.get(key) != value for key, value in expected.items()):
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

        # The settled frontend primitive owns title matching, active-corpus membership,
        # placement, section-registry metadata, and deterministic ordering. Passing the
        # captured context makes every returned Search fact belong to the same authority
        # identity that validates and signs the continuation cursor.
        facts = board.search_titles(
            query=query,
            projection_delay=_SEARCH_PROJECTION_DELAY,
            max_results=offset + page_size + 1,
            context=context,
        )
        visible = facts.results[offset : offset + page_size]
        has_more = facts.truncated or len(facts.results) > offset + page_size
        results: list[dict[str, Any]] = []
        for fact in visible:
            task_gid = session.scalar(
                select(models.TaskExternalAlias.external_id).where(
                    models.TaskExternalAlias.task_id == fact.task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            result = {
                "dish_id": str(fact.task_id),
                "title": fact.title,
                "section_id": str(fact.section_id),
                "section_label": fact.section_label,
                "workflow_role": fact.workflow_role,
                "project_label": fact.project_label,
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
                "results": results,
                "next_cursor": next_cursor,
                "page_size": page_size,
                "generation_id": str(context.generation_id),
                "registry_version_id": str(context.registry_version_id),
                "registry_revision": context.registry_revision,
            },
        )

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
                    result = PostgresCommandPort(
                        session,
                        cursor_secret=self._cursor_secret,
                    ).execute(
                        CommandCall(
                            command_name=command,
                            arguments=dict(arguments),
                            owner_id=principal.owner_id,
                            principal_class=principal_class,
                            run_id=run_id,
                            request_id=parsed_request_id,
                            now=datetime.now(timezone.utc),
                        )
                    )
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
            payload["data"] = data
            if principal.owner_id != self.config.action_client_id:
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
        except (CommandPortError, WorkflowAuthorityError) as exc:
            raise DishRuleError(
                "CONFLICT",
                str(exc),
                rule="postgresql_command_rejected",
            ) from exc
        except SQLAlchemyError as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL authority is unavailable; governed mutation was not admitted",
                rule="postgresql_authority_unavailable",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
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
            if view.operation_id is not None:
                result = error_envelope(
                    command,
                    DishRuleError(
                        "TASK_NOT_RESTING",
                        "Archive requires a resting Dish with no open workflow operation",
                        rule="archive_task_not_resting",
                        details={"open_operation_id": str(view.operation_id)},
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
        return self._execute_command(
            command,
            arguments,
            principal=principal,
            request_id=request_id,
            principal_class="admin",
        )
