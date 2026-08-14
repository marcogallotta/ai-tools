"""PostgreSQL authority adapter for the existing Dish HTTP service.

This module deliberately reuses ``dish-service`` and ``dish_service.http``. It
does not introduce another listener, routing table, authentication model, or
command framework. TEST rehearsal and post-cutover PROD authority therefore
share this service composition; environment/startup policy decides where it may
run.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping

from alembic.runtime.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from .command_contract import (
    ACTION_COMMANDS,
    COMMAND_DEFINITIONS,
    validate_postgres_action_request,
)
from .command_port import CommandCall, CommandPortError, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .openapi import postgres_action_openapi
from .workflow import RequestIdentityConflict, WorkflowAuthorityError


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

    _SUPPORTED_HTTP_SURFACES = frozenset({"admin", "agent", "action"})

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
        self.config = config
        self._engine = create_database_engine(DatabaseSettings(url=database_url))
        self._session_maker = session_factory(self._engine)
        self._cursor_secret = cursor_secret
        self._expected_database = expected_database
        self._expected_schema_head = expected_schema_head
        self._expected_release = expected_release
        self._expected_generation_id = expected_generation_id
        if profile not in {"test", "prod"}:
            raise ValueError("PostgreSQL runtime profile must be test or prod")
        self._profile = profile

    def close(self) -> None:
        self._engine.dispose()

    def supports_http_route(self, surface: str, command: str) -> bool:
        """Expose only routes implemented by the PostgreSQL authority adapter.

        The shared transport owns more private/admin routes than this adapter.
        The public Action surface is intentionally limited to the already
        implemented PostgreSQL command contract; unsupported legacy Action
        commands remain hidden rather than falling through to another backend.
        """

        definition = COMMAND_DEFINITIONS.get(command)
        if surface == "agent":
            return (
                definition is not None
                and definition.retained
                and definition.principal not in {"admin", "historical"}
            )
        if surface == "action":
            return command in ACTION_COMMANDS
        if surface == "admin":
            return (
                definition is not None
                and definition.retained
                and definition.principal == "admin"
            )
        if surface == "admin-lease":
            return command == "recover-lease"
        if surface == "admin-lease-expiry":
            return command == "expire-lease"
        return False

    def action_openapi(self, *, server_url: str) -> dict[str, Any]:
        """Return the PostgreSQL Action contract served by the shared listener."""

        return postgres_action_openapi(server_url=server_url)

    def validate_action_request(
        self, command: str, request: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate the PostgreSQL-specific no-Asana Action contract."""

        return validate_postgres_action_request(command, request)

    def _production_boundary(self, session, generation) -> dict[str, Any]:
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == generation.generation_id,
                models.AuthorityActivation.outcome == "activated",
            )
        )
        epoch = session.scalar(
            select(tx.ProjectionEpoch).where(
                tx.ProjectionEpoch.generation_id == generation.generation_id,
                tx.ProjectionEpoch.status == "active",
            )
        )
        if epoch is None:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "production PostgreSQL runtime requires an active projection epoch",
                rule="postgresql_production_projection_epoch_missing",
                retryable=False,
            )
        # The approved cutover order deploys the PostgreSQL runtime while
        # admission is still closed, before activation and rollback burn.  An
        # absent activation row is therefore a valid pre-burn deployment state.
        if activation is None:
            return {
                "phase": "pre_rollback_burn",
                "projection_epoch_id": str(epoch.projection_epoch_id),
                "external_effects_enabled": bool(epoch.external_effects_enabled),
            }
        if (
            activation.schema_head != self._expected_schema_head
            or activation.dish_release != self._expected_release
        ):
            raise DishRuleError(
                "BACKEND_REJECTED",
                "production PostgreSQL activation does not match the configured release binding",
                rule="postgresql_production_activation_identity_mismatch",
                retryable=False,
                details={
                    "expected_schema_head": self._expected_schema_head,
                    "observed_schema_head": activation.schema_head,
                    "expected_release": self._expected_release,
                    "observed_release": activation.dish_release,
                },
            )
        if (
            activation.rollback_burned_at is None
            or epoch.projection_epoch_id != activation.projection_epoch
            or epoch.external_effects_enabled
        ):
            raise DishRuleError(
                "BACKEND_REJECTED",
                "post-burn PostgreSQL authority requires the activated projection epoch with external effects disabled",
                rule="postgresql_production_projection_not_fenced",
                retryable=False,
            )
        return {
            "phase": "post_rollback_burn",
            "activation_id": str(activation.activation_id),
            "rollback_burned_at": activation.rollback_burned_at.isoformat(),
            "projection_epoch_id": str(epoch.projection_epoch_id),
            "external_effects_enabled": False,
        }

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
                        "PostgreSQL runtime database identity does not match the configured authority binding",
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
                production_boundary = None
                if self._profile == "prod":
                    production_boundary = self._production_boundary(session, generation)
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
                "PostgreSQL runtime identity does not match the configured authority binding",
                rule="postgresql_runtime_identity_mismatch",
                retryable=False,
                details={"expected": expected, "observed": observed},
            )
        if production_boundary is not None:
            observed["production_boundary"] = production_boundary
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

        envelope = error_envelope(command, error)
        envelope["data"]["request_id"] = request_id
        definition = COMMAND_DEFINITIONS.get(command)
        principal_class = (
            "admin"
            if definition is not None and definition.principal == "admin"
            else "agent"
        )
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
                        principal_class=principal_class,
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

    def _execute(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal_class: str,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
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
            return payload
        except CommandPortError as exc:
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
        return self._execute(
            command,
            arguments,
            principal_class="agent",
            principal=principal,
            request_id=request_id,
        )

    def execute_admin(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        definition = COMMAND_DEFINITIONS.get(command)
        if (
            definition is None
            or not definition.retained
            or definition.principal != "admin"
        ):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "command is not exposed on the PostgreSQL admin surface",
                rule="postgresql_admin_command_forbidden",
            )
        return self._execute(
            command,
            arguments,
            principal_class="admin",
            principal=principal,
            request_id=None if definition.profile == "Q" else request_id,
        )

    def _active_actor_lease_target(
        self,
        *,
        operation_id: uuid.UUID | None = None,
        lease_id: uuid.UUID | None = None,
        task_gid: str | None = None,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        try:
            with session_scope(self._session_maker) as session:
                if lease_id is not None:
                    leases = [session.get(wf.ServiceLease, lease_id)]
                else:
                    task_id = None
                    if task_gid is not None:
                        task_id = session.scalar(
                            select(models.TaskExternalAlias.task_id).where(
                                models.TaskExternalAlias.external_system == "asana",
                                models.TaskExternalAlias.external_id == task_gid,
                                models.TaskExternalAlias.state == "active",
                            )
                        )
                    statement = select(wf.ServiceLease).where(
                        wf.ServiceLease.lease_kind == "actor",
                        wf.ServiceLease.state == "active",
                    )
                    if operation_id is not None:
                        statement = statement.where(
                            wf.ServiceLease.operation_id == operation_id
                        )
                    elif task_id is not None:
                        statement = statement.where(wf.ServiceLease.task_id == task_id)
                    else:
                        leases = []
                        statement = None
                    if statement is not None:
                        leases = list(session.scalars(statement.limit(2)))
                exact = [lease for lease in leases if lease is not None]
                if len(exact) != 1 or exact[0].state != "active":
                    raise DishRuleError(
                        "CONFLICT",
                        "no exact active PostgreSQL actor lease matches the admin target",
                        rule="postgresql_admin_lease_target_not_exact",
                    )
                lease = exact[0]
                if lease.operation_id is None:
                    raise DishRuleError(
                        "CONFLICT",
                        "the active PostgreSQL actor lease is not operation-scoped",
                        rule="postgresql_admin_lease_operation_missing",
                    )
                return lease.lease_id, lease.operation_id
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

    def recover_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        try:
            operation_uuid = uuid.UUID(operation_id)
        except ValueError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "operation_id must be a canonical UUID",
                rule="service_identifier_invalid",
            ) from exc
        lease_id, exact_operation_id = self._active_actor_lease_target(
            operation_id=operation_uuid
        )
        return self.execute_admin(
            "recover-lease",
            {
                "operation_id": str(exact_operation_id),
                "lease_id": str(lease_id),
                "reason": reason,
            },
            principal=principal,
            request_id=request_id,
        )

    def expire_lease(
        self,
        principal: ServicePrincipal,
        *,
        lease_id: uuid.UUID | None = None,
        task_gid: str | None = None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        exact_lease_id, operation_id = self._active_actor_lease_target(
            lease_id=lease_id,
            task_gid=task_gid,
        )
        return self.execute_admin(
            "expire-lease",
            {
                "operation_id": str(operation_id),
                "lease_id": str(exact_lease_id),
                "reason": reason,
            },
            principal=principal,
            request_id=request_id,
        )
