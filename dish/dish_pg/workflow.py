"""Stage 3 transactional workflow authority services.

The service methods never commit. Callers own one SQLAlchemy transaction and can
compose request admission, execution, domain evidence, outcome, audit, causality,
and invocation-audit obligation atomically.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from . import reservation_models as reservations
from . import stage3_models as wf
from . import stage5_models as projection_models
from . import stage6_models as rel
from .recovery_rehydration import (
    RECOVERY_QUALIFICATION_REVISION,
    RECOVERY_READINESS_REVISION,
    RECOVERY_REHYDRATION_REVISION,
)
from .release_history import operation_revocation_history_reconciled


VALIDATION_FAILURE_REQUEST_KIND = "pre_execution_validation_failure"


class WorkflowAuthorityError(ValueError):
    """The requested authority transition is illegal or stale."""


class RequestIdentityConflict(WorkflowAuthorityError):
    """A request UUID was reused for a different logical request."""


class StaleAuthorityError(WorkflowAuthorityError):
    """A run, fence, claim, or generation is no longer current."""


class ContentionLost(WorkflowAuthorityError):
    """Another compatible transaction won the exclusive authority race."""


class OperationRunRevoked(StaleAuthorityError):
    """The exact operation/owner/run tuple has been permanently revoked."""


class ImportedRevocationHistoryUnreconciled(StaleAuthorityError):
    """Legacy imported operation lacks explicit exact-revocation reconciliation."""


class MutationAdmissionClosed(StaleAuthorityError):
    """Stage 6 has not opened PostgreSQL mutation admission."""


class FirstRequestReservationMismatch(StaleAuthorityError):
    """The first PostgreSQL mutation is not the exact reserved request."""


def canonical_json(value: Mapping[str, Any] | list[Any] | str | int | bool | None) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Mapping[str, Any] | list[Any] | str | int | bool | None) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestSpec:
    request_id: uuid.UUID
    generation_id: uuid.UUID
    run_id: uuid.UUID
    owner_id: str
    principal_class: str
    command_name: str
    canonical_payload: Mapping[str, Any]
    protocol_release: str
    dish_release: str
    admitted_at: datetime


@dataclass(frozen=True)
class StoredOutcome:
    outcome_id: uuid.UUID
    outcome_class: str
    result_code: str
    http_status: int
    result_payload: Mapping[str, Any]
    immutable_success: bool
    recorded_at: datetime


@dataclass(frozen=True)
class RequestAdmission:
    request: wf.ServiceRequest
    replayed: bool
    outcome: wf.ServiceRequestOutcome | None


@dataclass(frozen=True)
class ExecutionSpec:
    execution_id: uuid.UUID
    request_id: uuid.UUID
    generation_id: uuid.UUID
    task_id: uuid.UUID | None
    operation_id: uuid.UUID | None
    command_name: str
    transaction_profile: str
    canonical_intent: Mapping[str, Any]
    pinned_inputs: Mapping[str, Any]
    contract_binding_id: uuid.UUID
    admitted_at: datetime


class WorkflowAuthorityRepository:
    """Low-level Stage 3 authority operations in a caller-owned session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def require_active_generation(
        self,
        generation_id: uuid.UUID,
        *,
        hold_transition_fence: bool = False,
    ) -> models.AuthorityGeneration:
        if not hold_transition_fence:
            generation = self.session.get(models.AuthorityGeneration, generation_id)
        else:
            statement = select(models.AuthorityGeneration).where(
                models.AuthorityGeneration.generation_id == generation_id
            )
            if self.session.get_bind().dialect.name == "postgresql":
                # Consequential command transactions hold a shared generation-liveness fence
                # through the caller-owned commit. Generation rollover takes FOR UPDATE on
                # this same row, so either the command commits before the successor snapshot
                # or rollover wins and this fresh read observes the retired predecessor.
                statement = statement.with_for_update(read=True)
            statement = statement.execution_options(populate_existing=True)
            generation = self.session.scalar(statement)
        if generation is None or generation.status != "active":
            raise StaleAuthorityError("authority generation is not active")
        return generation

    def require_active_run(
        self, *, generation_id: uuid.UUID, run_id: uuid.UUID, owner_id: str | None = None
    ) -> wf.ServiceRun:
        self.require_active_generation(generation_id)
        run = self.session.get(wf.ServiceRun, run_id)
        if (
            run is None
            or run.generation_id != generation_id
            or run.status != "active"
            or (owner_id is not None and run.owner_id != owner_id)
        ):
            raise StaleAuthorityError("run is stale, retired, or belongs to another generation")
        return run

    def _locked_operation(
        self, *, generation_id: uuid.UUID, operation_id: uuid.UUID
    ) -> wf.WorkflowOperation:
        statement = select(wf.WorkflowOperation).where(
            wf.WorkflowOperation.operation_id == operation_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        operation = self.session.scalar(statement)
        if operation is None or operation.generation_id != generation_id:
            raise StaleAuthorityError("operation belongs to another authority generation")
        return operation

    def operation_run_revocation(
        self,
        *,
        generation_id: uuid.UUID,
        operation_id: uuid.UUID,
        owner_id: str,
        run_id: uuid.UUID,
        lock_operation: bool = False,
    ) -> wf.OperationRunRevocation | None:
        if lock_operation:
            self._locked_operation(generation_id=generation_id, operation_id=operation_id)
        live = self.session.scalar(
            select(wf.OperationRunRevocation).where(
                wf.OperationRunRevocation.generation_id == generation_id,
                wf.OperationRunRevocation.operation_id == operation_id,
                wf.OperationRunRevocation.owner_id == owner_id,
                wf.OperationRunRevocation.run_id == run_id,
            )
        )
        if live is not None:
            return live
        imported = self.session.scalars(
            select(wf.OperationRunRevocation).where(
                wf.OperationRunRevocation.generation_id == generation_id,
                wf.OperationRunRevocation.operation_id == operation_id,
                wf.OperationRunRevocation.owner_id == owner_id,
                wf.OperationRunRevocation.import_run_id.is_not(None),
            )
        )
        for row in imported:
            try:
                if uuid.UUID(row.source_run_id or "") == run_id:
                    return row
            except ValueError:
                continue
        return None

    def assert_operation_run_not_revoked(
        self,
        *,
        generation_id: uuid.UUID,
        operation_id: uuid.UUID,
        owner_id: str,
        run_id: uuid.UUID,
        lock_operation: bool = True,
    ) -> None:
        operation = (
            self._locked_operation(
                generation_id=generation_id, operation_id=operation_id
            )
            if lock_operation
            else self.session.get(wf.WorkflowOperation, operation_id)
        )
        if operation is None or operation.generation_id != generation_id:
            raise StaleAuthorityError("operation belongs to another authority generation")
        if not operation_revocation_history_reconciled(
            self.session, operation=operation
        ):
            raise ImportedRevocationHistoryUnreconciled(
                "legacy imported operation exact-run revocation history is unreconciled"
            )
        row = self.operation_run_revocation(
            generation_id=generation_id,
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            lock_operation=False,
        )
        if row is not None:
            raise OperationRunRevoked(
                "exact operation owner/run authority has been permanently revoked"
            )

    def revoke_operation_run(
        self,
        *,
        revocation_id: uuid.UUID,
        generation_id: uuid.UUID,
        operation_id: uuid.UUID,
        owner_id: str,
        run_id: uuid.UUID,
        reason: str,
        revoked_at: datetime,
        source_lease_id: uuid.UUID | None = None,
    ) -> wf.OperationRunRevocation:
        if not owner_id.strip() or not reason.strip():
            raise WorkflowAuthorityError("revocation owner and reason are required")
        self._locked_operation(generation_id=generation_id, operation_id=operation_id)
        run = self.session.get(wf.ServiceRun, run_id)
        if run is None or run.generation_id != generation_id or run.owner_id != owner_id:
            raise StaleAuthorityError("revocation target run/owner does not match the operation generation")
        existing = self.operation_run_revocation(
            generation_id=generation_id,
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            lock_operation=False,
        )
        if existing is not None:
            return existing
        active_execution = self.session.scalar(
            select(wf.CommandExecution.execution_id).where(
                wf.CommandExecution.generation_id == generation_id,
                wf.CommandExecution.operation_id == operation_id,
                wf.CommandExecution.status == "claimed",
            ).limit(1)
        )
        if active_execution is not None:
            raise ContentionLost(
                "operation has an active claimed execution; exact revocation lost the writer race"
            )
        if source_lease_id is not None:
            lease = self.session.get(wf.ServiceLease, source_lease_id)
            if (
                lease is None
                or lease.generation_id != generation_id
                or lease.operation_id != operation_id
                or lease.owner_id != owner_id
                or lease.run_id != run_id
            ):
                raise WorkflowAuthorityError(
                    "source lease does not prove the exact operation owner/run identity"
                )
        row = wf.OperationRunRevocation(
            revocation_id=revocation_id,
            generation_id=generation_id,
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            import_run_id=None,
            source_run_id=None,
            source_lease_id=source_lease_id,
            reason=reason.strip(),
            revoked_at=revoked_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def register_run(self, row: wf.ServiceRun) -> None:
        generation = self.require_active_generation(row.generation_id)
        bootstrap = None
        if generation.creation_reason == "destructive_restore" and row.bootstrap_id is None:
            raise StaleAuthorityError("restored generation requires external bootstrap authority")
        if row.bootstrap_id is not None:
            statement = select(models.GenerationBootstrapAuthority).where(
                models.GenerationBootstrapAuthority.bootstrap_id == row.bootstrap_id
            )
            if self.session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update().execution_options(populate_existing=True)
            bootstrap = self.session.scalar(statement)
            if bootstrap is None or bootstrap.generation_id != row.generation_id:
                raise StaleAuthorityError("bootstrap authority does not match generation")
            if bootstrap.retired_at is not None:
                raise StaleAuthorityError("bootstrap authority is retired")
            if bootstrap.consumed_at is not None:
                raise StaleAuthorityError("bootstrap authority is already consumed")
            if bootstrap.capability_digest != row.capability_digest:
                raise StaleAuthorityError("bootstrap authority capability does not match")
        self.session.add(row)
        self.session.flush()
        if generation.creation_reason == "destructive_restore":
            assert bootstrap is not None
            bootstrap.consumed_at = row.registered_at
            self.session.flush()

    @staticmethod
    def _validation_payload_matches_spec(
        payload: Mapping[str, Any], *, spec: RequestSpec
    ) -> bool:
        validation_error = payload.get("validation_error")
        errors = (
            validation_error.get("errors")
            if isinstance(validation_error, Mapping)
            else None
        )
        return (
            spec.principal_class in {"agent", "admin"}
            and payload.get("request_kind") == VALIDATION_FAILURE_REQUEST_KIND
            and payload.get("command") == spec.command_name
            and payload.get("owner_id") == spec.owner_id
            and payload.get("run_id") == str(spec.run_id)
            and isinstance(payload.get("arguments"), Mapping)
            and isinstance(validation_error, Mapping)
            and isinstance(validation_error.get("code"), str)
            and isinstance(validation_error.get("retryable"), bool)
            and isinstance(validation_error.get("message"), str)
            and isinstance(errors, list)
            and all(isinstance(item, Mapping) for item in errors)
        )

    @staticmethod
    def _validation_outcome_matches_identity(
        payload: Mapping[str, Any], *, outcome: StoredOutcome
    ) -> bool:
        validation_error = payload["validation_error"]
        result = outcome.result_payload
        data = result.get("data") if isinstance(result, Mapping) else None
        return (
            outcome.outcome_class == "rule_error"
            and outcome.result_code == validation_error["code"]
            and outcome.http_status == 400
            and outcome.immutable_success is False
            and isinstance(data, Mapping)
            and result.get("ok") is False
            and result.get("code") == validation_error["code"]
            and result.get("retryable") == validation_error["retryable"]
            and result.get("errors") == validation_error["errors"]
            and data.get("message") == validation_error["message"]
        )

    @staticmethod
    def _request_identity_matches(
        request: wf.ServiceRequest, *, spec: RequestSpec, payload_sha: str
    ) -> bool:
        return (
            request.generation_id == spec.generation_id
            and request.run_id == spec.run_id
            and request.owner_id == spec.owner_id
            and request.principal_class == spec.principal_class
            and request.command_name == spec.command_name
            and request.canonical_payload_sha256 == payload_sha
            and request.protocol_release == spec.protocol_release
            and request.dish_release == spec.dish_release
        )

    def _insert_validation_request(
        self, *, spec: RequestSpec, payload: Mapping[str, Any], payload_sha: str
    ) -> bool:
        values = {
            "request_id": spec.request_id,
            "generation_id": spec.generation_id,
            "run_id": spec.run_id,
            "owner_id": spec.owner_id,
            "principal_class": spec.principal_class,
            "command_name": spec.command_name,
            "canonical_payload_sha256": payload_sha,
            "canonical_payload": dict(payload),
            "protocol_release": spec.protocol_release,
            "dish_release": spec.dish_release,
            "admitted_at": spec.admitted_at,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(wf.ServiceRequest).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(wf.ServiceRequest).values(**values)
        else:
            raise WorkflowAuthorityError(
                "validation request persistence requires PostgreSQL or SQLite"
            )
        inserted_request_id = self.session.scalar(
            statement.on_conflict_do_nothing(
                index_elements=[wf.ServiceRequest.request_id]
            ).returning(wf.ServiceRequest.request_id)
        )
        self.session.flush()
        return inserted_request_id is not None

    def record_validation_failure(
        self,
        *,
        spec: RequestSpec,
        outcome: StoredOutcome,
        audit_event_id: uuid.UUID,
        audit_event_type: str,
        actor: str,
        audit_payload: Mapping[str, Any],
        obligation_id: uuid.UUID,
        invocation_metadata: Mapping[str, Any],
    ) -> RequestAdmission:
        """Bind and complete a pre-execution failure without mutation admission."""

        self.require_active_run(
            generation_id=spec.generation_id, run_id=spec.run_id, owner_id=spec.owner_id
        )
        payload = dict(spec.canonical_payload)
        if not self._validation_payload_matches_spec(payload, spec=spec):
            raise WorkflowAuthorityError(
                "validation request identity is incomplete or inconsistent"
            )
        if not self._validation_outcome_matches_identity(payload, outcome=outcome):
            raise WorkflowAuthorityError(
                "validation outcome does not match its stable error identity"
            )
        payload_sha = sha256_json(payload)
        inserted = self._insert_validation_request(
            spec=spec, payload=payload, payload_sha=payload_sha
        )
        request = self.session.get(wf.ServiceRequest, spec.request_id)
        if request is None:
            raise ContentionLost("validation request binding was not visible")
        if not self._request_identity_matches(request, spec=spec, payload_sha=payload_sha):
            raise RequestIdentityConflict("service request identity conflict")
        existing = self.session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == spec.request_id
            )
        )
        if not inserted:
            if existing is None:
                raise ContentionLost("request outcome is not yet authoritative")
            return RequestAdmission(request, True, existing)
        if existing is not None:
            raise ContentionLost("validation request unexpectedly had an outcome")
        recorded = self.record_outcome(
            request_id=spec.request_id,
            outcome=outcome,
            execution_id=None,
            audit_event_id=audit_event_id,
            audit_event_type=audit_event_type,
            actor=actor,
            audit_payload=audit_payload,
            task_id=None,
            operation_id=None,
            obligation_id=obligation_id,
            invocation_metadata=invocation_metadata,
        )
        return RequestAdmission(request, False, recorded)

    def admit_request(self, spec: RequestSpec) -> RequestAdmission:
        generation = self.require_active_generation(
            spec.generation_id, hold_transition_fence=True
        )
        self.require_active_run(
            generation_id=spec.generation_id, run_id=spec.run_id, owner_id=spec.owner_id
        )
        payload = dict(spec.canonical_payload)
        payload_sha = sha256_json(payload)
        existing = self.session.get(wf.ServiceRequest, spec.request_id)
        if existing is not None:
            if not self._request_identity_matches(
                existing, spec=spec, payload_sha=payload_sha
            ):
                raise RequestIdentityConflict("service request identity conflict")
            outcome = self.session.scalar(
                select(wf.ServiceRequestOutcome).where(
                    wf.ServiceRequestOutcome.request_id == existing.request_id
                )
            )
            return RequestAdmission(existing, True, outcome)

        candidate_exists = self.session.scalar(
            select(rel.ReleaseCandidate.candidate_id).where(
                rel.ReleaseCandidate.generation_id == spec.generation_id
            ).limit(1)
        ) is not None
        if generation.creation_reason == "destructive_restore" and not candidate_exists:
            bootstrap = self.session.scalar(
                select(models.GenerationBootstrapAuthority).where(
                    models.GenerationBootstrapAuthority.generation_id == generation.generation_id
                )
            )
            rehydration = self.session.scalar(
                select(models.AppliedMigrationEvent).where(
                    models.AppliedMigrationEvent.generation_id == spec.generation_id,
                    models.AppliedMigrationEvent.revision == RECOVERY_REHYDRATION_REVISION,
                    models.AppliedMigrationEvent.outcome == "repair",
                )
            )

            def recovery_lineage_valid(event, route: str) -> bool:
                details = None if event is None else event.details
                return (
                    details is not None
                    and bootstrap is not None
                    and details.get("route") == route
                    and details.get("external_restore_control_id")
                    == generation.external_restore_control_id
                    and details.get("predecessor_generation_id")
                    == str(generation.predecessor_generation_id)
                    and details.get("successor_generation_id") == str(generation.generation_id)
                    and details.get("bootstrap_id") == str(bootstrap.bootstrap_id)
                    and details.get("bootstrap_capability_sha256")
                    == bootstrap.capability_digest.hex()
                    and bootstrap.external_control_id
                    == generation.external_restore_control_id
                    and details.get("external_effects_enabled") is False
                )

            if not recovery_lineage_valid(rehydration, RECOVERY_REHYDRATION_REVISION):
                raise MutationAdmissionClosed(
                    "restored generation mutation admission requires deliberate reissue control"
                )
            qualification = self.session.scalar(
                select(models.AppliedMigrationEvent).where(
                    models.AppliedMigrationEvent.generation_id == spec.generation_id,
                    models.AppliedMigrationEvent.revision == RECOVERY_QUALIFICATION_REVISION,
                    models.AppliedMigrationEvent.outcome == "repair",
                )
            )
            readiness = self.session.scalar(
                select(models.AppliedMigrationEvent).where(
                    models.AppliedMigrationEvent.generation_id == spec.generation_id,
                    models.AppliedMigrationEvent.revision == RECOVERY_READINESS_REVISION,
                    models.AppliedMigrationEvent.outcome == "repair",
                )
            )
            qualification_valid = (
                recovery_lineage_valid(qualification, RECOVERY_QUALIFICATION_REVISION)
                and qualification.details.get("rehydration_event_id")
                == str(rehydration.migration_event_id)
                and qualification.details.get("protocol_release") == spec.protocol_release
                and qualification.details.get("dish_release") == spec.dish_release
            )
            readiness_valid = (
                recovery_lineage_valid(readiness, RECOVERY_READINESS_REVISION)
                and qualification_valid
                and readiness.details.get("rehydration_event_id")
                == str(rehydration.migration_event_id)
                and readiness.details.get("qualification_event_id")
                == str(qualification.migration_event_id)
                and readiness.details.get("qualification_request_id")
                == qualification.details.get("request_id")
                and readiness.details.get("ordinary_mutation_admission_open") is True
            )
            if not readiness_valid:
                epoch = self.session.scalar(
                    select(projection_models.ProjectionEpoch).where(
                        projection_models.ProjectionEpoch.generation_id
                        == generation.generation_id,
                        projection_models.ProjectionEpoch.status == "active",
                    )
                )
                exact_qualification_request = (
                    qualification_valid
                    and epoch is not None
                    and epoch.external_effects_enabled is False
                    and qualification.details.get("request_id") == str(spec.request_id)
                    and qualification.details.get("run_id") == str(spec.run_id)
                    and qualification.details.get("owner_id") == spec.owner_id
                    and qualification.details.get("principal_class") == spec.principal_class
                    and qualification.details.get("command_name") == spec.command_name
                    and qualification.details.get("canonical_payload_sha256") == payload_sha
                    and qualification.details.get("ordinary_mutation_admission_open") is False
                )
                if not exact_qualification_request:
                    raise MutationAdmissionClosed(
                        "restored generation mutation admission is closed pending recovery readiness"
                    )
        reservation = None
        if candidate_exists:
            control = self.session.get(rel.MutationAdmissionControl, spec.generation_id)
            if control is None:
                raise MutationAdmissionClosed("PostgreSQL mutation admission is closed")
            reservation = self.session.scalar(
                select(reservations.FirstRequestReservation)
                .where(
                    reservations.FirstRequestReservation.generation_id == spec.generation_id,
                    reservations.FirstRequestReservation.candidate_id == control.candidate_id,
                )
                .with_for_update()
            )
            cutover = (
                None
                if reservation is None
                else self.session.get(rel.CutoverRun, reservation.cutover_run_id)
            )
            if control.state == "open":
                if (
                    reservation is None
                    or reservation.state != "consumed"
                    or cutover is None
                    or cutover.candidate_id != control.candidate_id
                    or cutover.state not in {"first_admission_verified", "completed"}
                ):
                    raise MutationAdmissionClosed(
                        "open PostgreSQL mutation admission lacks a verified consumed first request"
                    )
                reservation = None
            elif control.state == "closed":
                if (
                    reservation is None
                    or cutover is None
                    or cutover.candidate_id != control.candidate_id
                    or cutover.state != "admission_open"
                ):
                    raise MutationAdmissionClosed(
                        "PostgreSQL mutation admission is closed pending first-request gate"
                    )
                if reservation.state != "reserved":
                    raise MutationAdmissionClosed(
                        "PostgreSQL mutation admission is closed pending first-admission verification"
                    )
                reserved_identity = (
                    reservation.request_id == spec.request_id
                    and reservation.command_name == spec.command_name
                    and reservation.owner_id == spec.owner_id
                    and reservation.principal_class == spec.principal_class
                    and reservation.run_id == spec.run_id
                    and reservation.canonical_payload_sha256 == payload_sha
                )
                if not reserved_identity:
                    raise FirstRequestReservationMismatch(
                        "first PostgreSQL mutation does not match the reserved request"
                    )
            else:
                raise MutationAdmissionClosed("PostgreSQL mutation admission is closed")

        row = wf.ServiceRequest(
            request_id=spec.request_id,
            generation_id=spec.generation_id,
            run_id=spec.run_id,
            owner_id=spec.owner_id,
            principal_class=spec.principal_class,
            command_name=spec.command_name,
            canonical_payload_sha256=payload_sha,
            canonical_payload=payload,
            protocol_release=spec.protocol_release,
            dish_release=spec.dish_release,
            admitted_at=spec.admitted_at,
        )
        self.session.add(row)
        try:
            self.session.flush()
            if reservation is not None:
                if self.session.bind.dialect.name == "postgresql":
                    self.session.refresh(reservation)
                else:
                    reservation.state = "consumed"
                    reservation.reservation_revision += 1
                    reservation.consumed_at = spec.admitted_at
                    self.session.flush()
        except IntegrityError as exc:
            if "mutation admission is closed" in str(exc).lower():
                raise MutationAdmissionClosed("PostgreSQL mutation admission is closed") from exc
            raise ContentionLost("concurrent request admission won") from exc
        return RequestAdmission(row, False, None)

    def begin_execution(self, spec: ExecutionSpec) -> wf.CommandExecution:
        request = self.session.get(wf.ServiceRequest, spec.request_id)
        if request is None or request.generation_id != spec.generation_id:
            raise WorkflowAuthorityError("execution requires matching admitted request")
        self.require_active_generation(spec.generation_id)
        binding = self.session.get(models.HonestContractBinding, spec.contract_binding_id)
        if binding is None or binding.dish_release != request.dish_release:
            raise WorkflowAuthorityError("execution requires matching Honest contract binding")
        row = wf.CommandExecution(
            execution_id=spec.execution_id,
            generation_id=spec.generation_id,
            request_id=spec.request_id,
            task_id=spec.task_id,
            operation_id=spec.operation_id,
            command_name=spec.command_name,
            transaction_profile=spec.transaction_profile,
            canonical_intent=dict(spec.canonical_intent),
            pinned_inputs=dict(spec.pinned_inputs),
            contract_binding_id=spec.contract_binding_id,
            status="pending",
            claim_owner=None,
            claim_token=None,
            claim_expires_at=None,
            execution_revision=1,
            admitted_at=spec.admitted_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def claim_execution(
        self,
        *,
        execution_id: uuid.UUID,
        claimant: str,
        claim_token: uuid.UUID,
        now: datetime,
        ttl: timedelta,
    ) -> wf.CommandExecution:
        execution = self.session.get(wf.CommandExecution, execution_id)
        if execution is None:
            raise WorkflowAuthorityError("unknown command execution")
        self.require_active_generation(execution.generation_id)
        if execution.operation_id is not None:
            request = self.session.get(wf.ServiceRequest, execution.request_id)
            if request is None:
                raise WorkflowAuthorityError("command execution has no admitted request")
            self.assert_operation_run_not_revoked(
                generation_id=execution.generation_id,
                operation_id=execution.operation_id,
                owner_id=request.owner_id,
                run_id=request.run_id,
                lock_operation=True,
            )
        previous_revision = execution.execution_revision
        allowed = execution.status == "pending" or (
            execution.status == "claimed"
            and execution.claim_expires_at is not None
            and execution.claim_expires_at <= now
        )
        if not allowed:
            raise ContentionLost("execution is already claimed or terminal")
        prior_status = execution.status
        claim_expiry = now + ttl
        result = self.session.execute(
            update(wf.CommandExecution)
            .where(
                wf.CommandExecution.execution_id == execution_id,
                wf.CommandExecution.execution_revision == previous_revision,
                wf.CommandExecution.status == prior_status,
            )
            .values(
                status="claimed",
                claim_owner=claimant,
                claim_token=claim_token,
                claim_expires_at=claim_expiry,
                execution_revision=previous_revision + 1,
            )
        )
        if result.rowcount != 1:
            raise ContentionLost("execution claim lost to a concurrent claimant")
        self.session.add(
            wf.ExecutionClaimEvent(
                claim_event_id=uuid.uuid4(),
                execution_id=execution_id,
                claim_token=claim_token,
                event_kind="claimed" if prior_status == "pending" else "taken_over",
                claimant=claimant,
                expected_execution_revision=previous_revision,
                claim_expires_at=claim_expiry,
                occurred_at=now,
            )
        )
        self.session.flush()
        self.session.expire(execution)
        return execution

    def capture_task_fence(
        self, *, execution_id: uuid.UUID, generation_id: uuid.UUID, task_id: uuid.UUID, at: datetime
    ) -> wf.TaskExecutionFence:
        state = self.session.get(models.DishState, (generation_id, task_id))
        membership = self.session.get(models.TaskMembershipHead, (generation_id, task_id))
        if state is None or membership is None:
            raise WorkflowAuthorityError("task has incomplete scalar/membership authority")
        row = wf.TaskExecutionFence(
            execution_id=execution_id,
            generation_id=generation_id,
            task_id=task_id,
            expected_dish_version=state.dish_version,
            expected_membership_revision=membership.membership_revision,
            captured_at=at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def assert_task_fence(self, execution_id: uuid.UUID) -> models.DishState:
        fence = self.session.get(wf.TaskExecutionFence, execution_id)
        if fence is None:
            raise WorkflowAuthorityError("execution has no task fence")
        state_statement = select(models.DishState).where(
            models.DishState.generation_id == fence.generation_id,
            models.DishState.task_id == fence.task_id,
        )
        membership_statement = select(models.TaskMembershipHead).where(
            models.TaskMembershipHead.generation_id == fence.generation_id,
            models.TaskMembershipHead.task_id == fence.task_id,
        )
        if self.session.get_bind().dialect.name == "postgresql":
            # Match ScalarDishMutation's lock order and hold both task-authority rows
            # through the caller-owned commit. A competing scalar transition either
            # commits first and is observed by these fresh reads, or waits until this
            # command has finished its task mutation.
            state_statement = state_statement.with_for_update()
            membership_statement = membership_statement.with_for_update()
        state = self.session.scalar(
            state_statement.execution_options(populate_existing=True)
        )
        membership = self.session.scalar(
            membership_statement.execution_options(populate_existing=True)
        )
        if (
            state is None
            or membership is None
            or state.dish_version != fence.expected_dish_version
            or membership.membership_revision != fence.expected_membership_revision
        ):
            raise StaleAuthorityError("task fence is stale")
        return state

    def capture_operation_fence(
        self, *, execution_id: uuid.UUID, operation_id: uuid.UUID, at: datetime
    ) -> wf.OperationExecutionFence:
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if operation is None:
            raise WorkflowAuthorityError("unknown workflow operation")
        row = wf.OperationExecutionFence(
            execution_id=execution_id,
            operation_id=operation_id,
            expected_operation_revision=operation.operation_revision,
            expected_phase=operation.phase,
            captured_at=at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def assert_operation_fence(self, execution_id: uuid.UUID) -> wf.WorkflowOperation:
        fence = self.session.get(wf.OperationExecutionFence, execution_id)
        if fence is None:
            raise WorkflowAuthorityError("execution has no operation fence")
        operation = self.session.get(wf.WorkflowOperation, fence.operation_id)
        if operation is None or (
            operation.operation_revision != fence.expected_operation_revision
            or operation.phase != fence.expected_phase
        ):
            raise StaleAuthorityError("operation fence is stale")
        return operation

    def record_outcome(
        self,
        *,
        request_id: uuid.UUID,
        outcome: StoredOutcome,
        execution_id: uuid.UUID | None,
        audit_event_id: uuid.UUID,
        audit_event_type: str,
        actor: str,
        audit_payload: Mapping[str, Any],
        task_id: uuid.UUID | None,
        operation_id: uuid.UUID | None,
        obligation_id: uuid.UUID,
        invocation_metadata: Mapping[str, Any],
    ) -> wf.ServiceRequestOutcome:
        request = self.session.get(wf.ServiceRequest, request_id)
        if request is None:
            raise WorkflowAuthorityError("cannot complete an unknown request")
        existing = self.session.scalar(
            select(wf.ServiceRequestOutcome).where(wf.ServiceRequestOutcome.request_id == request_id)
        )
        if existing is not None:
            return existing
        payload = dict(outcome.result_payload)
        row = wf.ServiceRequestOutcome(
            outcome_id=outcome.outcome_id,
            request_id=request_id,
            outcome_class=outcome.outcome_class,
            result_code=outcome.result_code,
            http_status=outcome.http_status,
            result_payload=payload,
            result_sha256=sha256_json(payload),
            immutable_success=outcome.immutable_success,
            recorded_at=outcome.recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        if execution_id is not None:
            execution = self.session.get(wf.CommandExecution, execution_id)
            if execution is None or execution.request_id != request_id:
                raise WorkflowAuthorityError("outcome execution does not own request")
            execution.status = (
                "committed"
                if outcome.outcome_class == "success"
                else "uncertain"
                if outcome.outcome_class == "uncertain"
                else "failed"
            )
            execution.claim_owner = None
            execution.claim_token = None
            execution.claim_expires_at = None
            execution.execution_revision += 1
            execution.terminal_at = outcome.recorded_at
        self.session.add(
            wf.GovernedAuditEvent(
                audit_event_id=audit_event_id,
                generation_id=request.generation_id,
                request_id=request_id,
                command_execution_id=execution_id,
                task_id=task_id,
                operation_id=operation_id,
                event_type=audit_event_type,
                actor=actor,
                payload=dict(audit_payload),
                occurred_at=outcome.recorded_at,
            )
        )
        invocation_payload = {
            "request_id": str(request_id),
            "outcome_id": str(outcome.outcome_id),
            "metadata": dict(invocation_metadata),
        }
        self.session.add(
            wf.InvocationAuditObligation(
                obligation_id=obligation_id,
                generation_id=request.generation_id,
                request_id=request_id,
                outcome_id=outcome.outcome_id,
                command_execution_id=execution_id,
                payload_sha256=sha256_json(invocation_payload),
                required_metadata=dict(invocation_metadata),
                state="pending",
                created_at=outcome.recorded_at,
                terminal_at=None,
            )
        )
        self.session.flush()
        return row


class WorkflowAuthorityService:
    """Stage 3 domain orchestration with caller-owned transaction boundaries."""

    def __init__(
        self,
        session: Session,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.repo = WorkflowAuthorityRepository(session)

    def register_run(
        self,
        *,
        run_id: uuid.UUID,
        generation_id: uuid.UUID,
        owner_id: str,
        agent: str,
        capability_digest: bytes,
        registered_at: datetime,
        bootstrap_id: uuid.UUID | None = None,
    ) -> wf.ServiceRun:
        row = wf.ServiceRun(
            run_id=run_id,
            generation_id=generation_id,
            owner_id=owner_id,
            agent=agent,
            capability_digest=capability_digest,
            bootstrap_id=bootstrap_id,
            status="active",
            registered_at=registered_at,
            retired_at=None,
        )
        self.repo.register_run(row)
        return row

    def admit_request(self, spec: RequestSpec) -> RequestAdmission:
        return self.repo.admit_request(spec)

    def record_validation_failure(
        self,
        *,
        spec: RequestSpec,
        outcome: StoredOutcome,
        audit_event_id: uuid.UUID,
        audit_event_type: str,
        actor: str,
        audit_payload: Mapping[str, Any],
        obligation_id: uuid.UUID,
        invocation_metadata: Mapping[str, Any],
    ) -> RequestAdmission:
        return self.repo.record_validation_failure(
            spec=spec,
            outcome=outcome,
            audit_event_id=audit_event_id,
            audit_event_type=audit_event_type,
            actor=actor,
            audit_payload=audit_payload,
            obligation_id=obligation_id,
            invocation_metadata=invocation_metadata,
        )

    def begin_execution(self, spec: ExecutionSpec) -> wf.CommandExecution:
        return self.repo.begin_execution(spec)

    def create_operation(
        self,
        *,
        operation_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        kind: str,
        phase: str,
        persisted_actions: list[str],
        created_at: datetime,
        predecessor_operation_id: uuid.UUID | None = None,
    ) -> wf.WorkflowOperation:
        execution = self.session.get(wf.CommandExecution, execution_id)
        if execution is None or execution.task_id != task_id:
            raise WorkflowAuthorityError("operation requires matching command execution")
        self.repo.assert_task_fence(execution_id)
        row = wf.WorkflowOperation(
            operation_id=operation_id,
            generation_id=execution.generation_id,
            task_id=task_id,
            kind=kind,
            lifecycle="open",
            phase=phase,
            persisted_actions=list(persisted_actions),
            creation_request_id=execution.request_id,
            creation_execution_id=execution_id,
            contract_binding_id=execution.contract_binding_id,
            predecessor_operation_id=predecessor_operation_id,
            terminal_outcome=None,
            operation_revision=1,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        execution.operation_id = operation_id
        self.session.flush()
        return row

    def issue_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        issuing_request_id: uuid.UUID,
        task_id: uuid.UUID,
        issued_at: datetime,
    ) -> wf.PlanningIntentChallenge:
        request = self.session.get(wf.ServiceRequest, issuing_request_id)
        if request is None or request.command_name != "start":
            raise WorkflowAuthorityError("planning challenge requires an admitted start request")
        run = self.repo.require_active_run(
            generation_id=request.generation_id, run_id=request.run_id, owner_id=request.owner_id
        )
        row = wf.PlanningIntentChallenge(
            challenge_id=challenge_id,
            generation_id=request.generation_id,
            issuing_request_id=issuing_request_id,
            task_id=task_id,
            run_id=request.run_id,
            owner_id=request.owner_id,
            agent=run.agent,
            target_kind="planning",
            state="issued",
            claiming_request_id=None,
            intent_basis=None,
            override_reason=None,
            resulting_operation_id=None,
            settled_by=None,
            settlement_reason=None,
            issued_at=issued_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def claim_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        claiming_request_id: uuid.UUID,
        intent_basis: str,
        override_reason: str | None,
    ) -> wf.PlanningIntentChallenge:
        challenge = self.session.get(wf.PlanningIntentChallenge, challenge_id)
        request = self.session.get(wf.ServiceRequest, claiming_request_id)
        if challenge is None or request is None:
            raise WorkflowAuthorityError("unknown challenge or claiming request")
        if challenge.state != "issued":
            raise ContentionLost("planning challenge is no longer issuable")
        if (
            request.generation_id != challenge.generation_id
            or request.run_id != challenge.run_id
            or request.owner_id != challenge.owner_id
            or request.command_name != "start"
        ):
            raise WorkflowAuthorityError("claiming request does not match issued challenge")
        if intent_basis not in {"user_requested", "agent_override"}:
            raise WorkflowAuthorityError("invalid planning intent basis")
        if intent_basis == "agent_override" and not (override_reason or "").strip():
            raise WorkflowAuthorityError("agent override requires a reason")
        result = self.session.execute(
            update(wf.PlanningIntentChallenge)
            .where(
                wf.PlanningIntentChallenge.challenge_id == challenge_id,
                wf.PlanningIntentChallenge.state == "issued",
            )
            .values(
                state="claimed",
                claiming_request_id=claiming_request_id,
                intent_basis=intent_basis,
                override_reason=override_reason,
            )
        )
        if result.rowcount != 1:
            raise ContentionLost("planning challenge claim lost")
        self.session.flush()
        self.session.expire(challenge)
        return challenge

    def consume_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        operation_id: uuid.UUID,
        consumed_at: datetime,
    ) -> wf.PlanningIntentChallenge:
        challenge = self.session.get(wf.PlanningIntentChallenge, challenge_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if challenge is None or operation is None:
            raise WorkflowAuthorityError("unknown challenge or operation")
        if challenge.state != "claimed" or challenge.task_id != operation.task_id:
            raise WorkflowAuthorityError("challenge is not claimable for this operation")
        challenge.state = "consumed"
        challenge.resulting_operation_id = operation_id
        challenge.terminal_at = consumed_at
        self.session.flush()
        return challenge

    def settle_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        actor: str,
        reason: str,
        settled_at: datetime,
    ) -> wf.PlanningIntentChallenge:
        challenge = self.session.get(wf.PlanningIntentChallenge, challenge_id)
        if challenge is None or challenge.state != "issued":
            raise WorkflowAuthorityError("only an issued challenge may be settled")
        if not reason.strip():
            raise WorkflowAuthorityError("settlement reason is required")
        challenge.state = "settled"
        challenge.settled_by = actor
        challenge.settlement_reason = reason
        challenge.terminal_at = settled_at
        self.session.flush()
        return challenge

    def acquire_actor_lease(
        self,
        *,
        lease_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_id: str,
        actor_role: str,
        actor_attempt_sequence: int,
        issued_at: datetime,
        expires_at: datetime,
        verification_cycle_id: uuid.UUID | None = None,
    ) -> wf.ServiceLease:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("lease requires matching execution and operation")
        self.repo.require_active_run(
            generation_id=execution.generation_id, run_id=run_id, owner_id=owner_id
        )
        self.repo.assert_operation_run_not_revoked(
            generation_id=execution.generation_id,
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            lock_operation=True,
        )
        row = wf.ServiceLease(
            lease_id=lease_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            run_id=run_id,
            owner_id=owner_id,
            lease_kind="actor",
            actor_role=actor_role,
            actor_attempt_sequence=actor_attempt_sequence,
            verification_cycle_id=verification_cycle_id,
            state="active",
            issued_at=issued_at,
            expires_at=expires_at,
            lease_revision=1,
            terminal_at=None,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ContentionLost("another active actor lease or attempt sequence already exists") from exc
        self.session.add(
            wf.LeaseEvent(
                lease_event_id=self.uuid_factory(),
                lease_id=lease_id,
                event_kind="issued",
                request_id=execution.request_id,
                command_execution_id=execution_id,
                prior_revision=0,
                resulting_revision=1,
                prior_expiry=issued_at,
                resulting_expiry=expires_at,
                reason="actor authority issued",
                occurred_at=issued_at,
            )
        )
        self.session.flush()
        return row

    def renew_lease(
        self,
        *,
        lease_id: uuid.UUID,
        execution_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_id: str,
        now: datetime,
        new_expiry: datetime,
    ) -> wf.ServiceLease:
        lease = self.session.get(wf.ServiceLease, lease_id)
        execution = self.session.get(wf.CommandExecution, execution_id)
        if lease is None or execution is None:
            raise WorkflowAuthorityError("unknown lease or execution")
        if execution.generation_id != lease.generation_id:
            raise StaleAuthorityError("lease and execution belong to different generations")
        self.repo.require_active_run(
            generation_id=lease.generation_id, run_id=run_id, owner_id=owner_id
        )
        if lease.operation_id is not None:
            self.repo.assert_operation_run_not_revoked(
                generation_id=lease.generation_id,
                operation_id=lease.operation_id,
                owner_id=owner_id,
                run_id=run_id,
                lock_operation=True,
            )
        if (
            lease.state != "active"
            or lease.run_id != run_id
            or lease.owner_id != owner_id
            or lease.expires_at <= now
            or new_expiry <= now
        ):
            raise StaleAuthorityError("lease is expired or caller does not own it")
        prior_revision = lease.lease_revision
        prior_expiry = lease.expires_at
        lease.expires_at = new_expiry
        lease.lease_revision += 1
        self.session.add(
            wf.LeaseEvent(
                lease_event_id=self.uuid_factory(),
                lease_id=lease_id,
                event_kind="renewed",
                request_id=execution.request_id,
                command_execution_id=execution_id,
                prior_revision=prior_revision,
                resulting_revision=prior_revision + 1,
                prior_expiry=prior_expiry,
                resulting_expiry=new_expiry,
                reason="owner renewal",
                occurred_at=now,
            )
        )
        self.session.flush()
        return lease

    def record_inspection(
        self,
        *,
        inspection_id: uuid.UUID,
        execution_id: uuid.UUID,
        cycle_id: uuid.UUID,
        actor_fact_id: uuid.UUID,
        verifier_run_id: uuid.UUID,
        attestation: str,
        inspected_at: datetime,
    ) -> wf.VerificationInspectionOccurrence:
        execution = self.session.get(wf.CommandExecution, execution_id)
        cycle = self.session.get(wf.VerificationCycle, cycle_id)
        actor = self.session.get(wf.OperationActorFact, actor_fact_id)
        if execution is None or cycle is None or actor is None:
            raise WorkflowAuthorityError("inspection authority is incomplete")
        if cycle.lifecycle != "open" or actor.operation_id != cycle.operation_id:
            raise WorkflowAuthorityError("inspection actor/cycle mismatch")
        if actor.run_id != verifier_run_id or not attestation.strip():
            raise WorkflowAuthorityError("inspection requires exact verifier run and attestation")
        placement = self.session.get(models.DishState, (cycle.generation_id, cycle.task_id))
        if placement is None:
            raise WorkflowAuthorityError("inspection requires current placement evidence")
        row = wf.VerificationInspectionOccurrence(
            inspection_id=inspection_id,
            cycle_id=cycle_id,
            operation_id=cycle.operation_id,
            generation_id=cycle.generation_id,
            task_id=cycle.task_id,
            reviewed_content_version_id=cycle.reviewed_content_version_id,
            verifier_actor_fact_id=actor_fact_id,
            verifier_run_id=verifier_run_id,
            attestation=attestation,
            section_id=placement.section_id,
            registry_version_id=placement.registry_version_id,
            placement_version=placement.placement_version,
            request_id=execution.request_id,
            command_execution_id=execution_id,
            inspected_at=inspected_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_actor_fact(
        self,
        *,
        actor_fact_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_id: str,
        actor_role: str,
        agent: str,
        actor_attempt_sequence: int,
        recorded_at: datetime,
    ) -> wf.OperationActorFact:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("actor fact requires matching execution and operation")
        self.repo.require_active_run(
            generation_id=execution.generation_id, run_id=run_id, owner_id=owner_id
        )
        self.repo.assert_operation_run_not_revoked(
            generation_id=execution.generation_id,
            operation_id=operation_id,
            owner_id=owner_id,
            run_id=run_id,
            lock_operation=True,
        )
        row = wf.OperationActorFact(
            actor_fact_id=actor_fact_id,
            operation_id=operation_id,
            task_id=operation.task_id,
            actor_role=actor_role,
            agent=agent,
            owner_id=owner_id,
            run_id=run_id,
            actor_attempt_sequence=actor_attempt_sequence,
            command_execution_id=execution_id,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def grant_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        operation_id: uuid.UUID | None,
        field_name: str,
        before_value: Any,
        after_value: Any,
        reason: str,
        actor: str,
        run_id: uuid.UUID,
        granted_at: datetime,
    ) -> wf.MarcoAuthorizationGrant:
        execution = self.session.get(wf.CommandExecution, execution_id)
        if execution is None or execution.task_id != task_id:
            raise WorkflowAuthorityError("authorization requires matching command execution")
        request = self.session.get(wf.ServiceRequest, execution.request_id)
        if request is None or request.principal_class != "admin":
            raise WorkflowAuthorityError("only an admitted admin request may grant authority")
        if not reason.strip():
            raise WorkflowAuthorityError("authorization reason is required")
        grant = wf.MarcoAuthorizationGrant(
            grant_id=grant_id,
            generation_id=execution.generation_id,
            task_id=task_id,
            operation_id=operation_id,
            field_name=field_name,
            before_value=before_value,
            after_value=after_value,
            reason=reason,
            actor=actor,
            run_id=run_id,
            request_id=execution.request_id,
            command_execution_id=execution_id,
            granted_at=granted_at,
        )
        self.session.add(grant)
        self.session.flush()
        self.session.add(
            wf.MarcoAuthorizationState(
                grant_id=grant_id,
                state="available",
                reservation_token=None,
                reservation_request_id=None,
                consumed_result_id=None,
                authorization_revision=1,
                updated_at=granted_at,
            )
        )
        self.session.flush()
        return grant

    def reserve_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        reservation_token: uuid.UUID,
        execution_id: uuid.UUID,
        reserved_at: datetime,
    ) -> wf.MarcoAuthorizationState:
        execution = self.session.get(wf.CommandExecution, execution_id)
        state = self.session.get(wf.MarcoAuthorizationState, grant_id)
        if execution is None or state is None:
            raise WorkflowAuthorityError("unknown authorization or execution")
        expected_revision = state.authorization_revision
        result = self.session.execute(
            update(wf.MarcoAuthorizationState)
            .where(
                wf.MarcoAuthorizationState.grant_id == grant_id,
                wf.MarcoAuthorizationState.state == "available",
                wf.MarcoAuthorizationState.authorization_revision == expected_revision,
            )
            .values(
                state="reserved",
                reservation_token=reservation_token,
                reservation_request_id=execution.request_id,
                authorization_revision=expected_revision + 1,
                updated_at=reserved_at,
            )
        )
        if result.rowcount != 1:
            raise ContentionLost("authorization reservation lost")
        self.session.add(
            wf.MarcoAuthorizationEvent(
                authorization_event_id=self.uuid_factory(),
                grant_id=grant_id,
                event_kind="reserved",
                reservation_token=reservation_token,
                request_id=execution.request_id,
                command_execution_id=execution_id,
                bound_result_id=None,
                occurred_at=reserved_at,
            )
        )
        self.session.flush()
        self.session.expire(state)
        return state

    def release_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        reservation_token: uuid.UUID,
        execution_id: uuid.UUID,
        released_at: datetime,
    ) -> wf.MarcoAuthorizationState:
        execution = self.session.get(wf.CommandExecution, execution_id)
        state = self.session.get(wf.MarcoAuthorizationState, grant_id)
        if execution is None or state is None:
            raise WorkflowAuthorityError("unknown authorization or execution")
        if state.state != "reserved" or state.reservation_token != reservation_token:
            raise StaleAuthorityError("authorization reservation does not match")
        revision = state.authorization_revision
        state.state = "available"
        state.reservation_token = None
        state.reservation_request_id = None
        state.authorization_revision = revision + 1
        state.updated_at = released_at
        self.session.add(
            wf.MarcoAuthorizationEvent(
                authorization_event_id=self.uuid_factory(),
                grant_id=grant_id,
                event_kind="released",
                reservation_token=reservation_token,
                request_id=execution.request_id,
                command_execution_id=execution_id,
                bound_result_id=None,
                occurred_at=released_at,
            )
        )
        self.session.flush()
        return state

    def consume_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        reservation_token: uuid.UUID,
        execution_id: uuid.UUID,
        bound_result_id: uuid.UUID,
        consumed_at: datetime,
    ) -> wf.MarcoAuthorizationState:
        execution = self.session.get(wf.CommandExecution, execution_id)
        state = self.session.get(wf.MarcoAuthorizationState, grant_id)
        if execution is None or state is None:
            raise WorkflowAuthorityError("unknown authorization or execution")
        if state.state != "reserved" or state.reservation_token != reservation_token:
            raise StaleAuthorityError("authorization reservation does not match")
        state.state = "consumed"
        state.consumed_result_id = bound_result_id
        state.authorization_revision += 1
        state.updated_at = consumed_at
        self.session.add(
            wf.MarcoAuthorizationEvent(
                authorization_event_id=self.uuid_factory(),
                grant_id=grant_id,
                event_kind="consumed",
                reservation_token=reservation_token,
                request_id=execution.request_id,
                command_execution_id=execution_id,
                bound_result_id=bound_result_id,
                occurred_at=consumed_at,
            )
        )
        self.session.flush()
        return state

    def open_verification_cycle(
        self,
        *,
        cycle_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        reviewed_content_version_id: uuid.UUID,
        created_at: datetime,
    ) -> wf.VerificationCycle:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        version = self.session.get(models.ContentVersion, reviewed_content_version_id)
        if execution is None or operation is None or version is None:
            raise WorkflowAuthorityError("verification cycle authority is incomplete")
        if execution.task_id != operation.task_id or version.task_id != operation.task_id:
            raise WorkflowAuthorityError("verification cycle task mismatch")
        cycle_sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(wf.VerificationCycle.cycle_sequence), 0)).where(
                    wf.VerificationCycle.operation_id == operation_id
                )
            )
            or 0
        ) + 1
        row = wf.VerificationCycle(
            cycle_id=cycle_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            reviewed_content_version_id=reviewed_content_version_id,
            contract_binding_id=execution.contract_binding_id,
            cycle_sequence=cycle_sequence,
            lifecycle="open",
            outcome=None,
            created_by_execution_id=execution_id,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def signoff_verification(
        self,
        *,
        signoff_id: uuid.UUID,
        execution_id: uuid.UUID,
        cycle_id: uuid.UUID,
        inspection_id: uuid.UUID,
        signed_content_version_id: uuid.UUID,
        signoff_kind: str,
        signed_at: datetime,
        inherited_from_signoff_id: uuid.UUID | None = None,
    ) -> wf.VerificationSignoff:
        execution = self.session.get(wf.CommandExecution, execution_id)
        cycle = self.session.get(wf.VerificationCycle, cycle_id)
        inspection = self.session.get(wf.VerificationInspectionOccurrence, inspection_id)
        if execution is None or cycle is None or inspection is None:
            raise WorkflowAuthorityError("signoff authority is incomplete")
        if cycle.lifecycle != "open" or inspection.cycle_id != cycle_id:
            raise WorkflowAuthorityError("signoff inspection/cycle mismatch")
        if inspection.reviewed_content_version_id != signed_content_version_id:
            correction = self.session.scalar(
                select(wf.VerificationCorrection).where(
                    wf.VerificationCorrection.cycle_id == cycle_id,
                    wf.VerificationCorrection.source_content_version_id
                    == inspection.reviewed_content_version_id,
                    wf.VerificationCorrection.corrected_content_version_id
                    == signed_content_version_id,
                )
            )
            signed = self.session.get(models.ContentVersion, signed_content_version_id)
            direct_status_transition = (
                signoff_kind == "direct"
                and signed is not None
                and signed.generation_id == cycle.generation_id
                and signed.task_id == cycle.task_id
                and signed.predecessor_content_version_id
                == inspection.reviewed_content_version_id
            )
            if correction is None and not direct_status_transition:
                raise WorkflowAuthorityError(
                    "signoff must bind the inspected occurrence, its exact canonical status transition, or its recorded correction"
                )
        row = wf.VerificationSignoff(
            signoff_id=signoff_id,
            cycle_id=cycle_id,
            task_id=cycle.task_id,
            signed_content_version_id=signed_content_version_id,
            inspection_id=inspection_id,
            verifier_actor_fact_id=inspection.verifier_actor_fact_id,
            inherited_from_signoff_id=inherited_from_signoff_id,
            signoff_kind=signoff_kind,
            command_execution_id=execution_id,
            signed_at=signed_at,
        )
        self.session.add(row)
        cycle.lifecycle = "approved"
        cycle.outcome = "approved"
        cycle.terminal_at = signed_at
        self.session.flush()
        return row

    def open_evidence_hold(
        self,
        *,
        hold_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        baseline_content_version_id: uuid.UUID,
        reason: str,
        opened_at: datetime,
        cycle_id: uuid.UUID | None = None,
    ) -> wf.EvidenceHold:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("hold requires matching execution and operation")
        row = wf.EvidenceHold(
            hold_id=hold_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            cycle_id=cycle_id,
            baseline_content_version_id=baseline_content_version_id,
            state="open",
            reason=reason,
            opened_by_execution_id=execution_id,
            opened_at=opened_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add(
            wf.EvidenceHoldEvent(
                hold_event_id=self.uuid_factory(),
                hold_id=hold_id,
                event_kind="opened",
                evidence_payload={"reason": reason},
                request_id=execution.request_id,
                command_execution_id=execution_id,
                occurred_at=opened_at,
            )
        )
        self.session.flush()
        return row

    def supply_evidence(
        self,
        *,
        hold_id: uuid.UUID,
        execution_id: uuid.UUID,
        evidence_payload: Mapping[str, Any],
        supplied_at: datetime,
    ) -> wf.EvidenceHold:
        hold = self.session.get(wf.EvidenceHold, hold_id)
        execution = self.session.get(wf.CommandExecution, execution_id)
        if hold is None or execution is None or hold.state != "open":
            raise WorkflowAuthorityError("evidence hold is not open")
        hold.state = "supplied"
        hold.terminal_at = supplied_at
        self.session.add(
            wf.EvidenceHoldEvent(
                hold_event_id=self.uuid_factory(),
                hold_id=hold_id,
                event_kind="supplied",
                evidence_payload=dict(evidence_payload),
                request_id=execution.request_id,
                command_execution_id=execution_id,
                occurred_at=supplied_at,
            )
        )
        self.session.flush()
        return hold

    def open_human_review(
        self,
        *,
        requirement_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        route: str,
        question: str,
        baseline_content_version_id: uuid.UUID,
        opened_at: datetime,
        cycle_id: uuid.UUID | None = None,
    ) -> wf.HumanReviewRequirement:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("human review requires matching execution and operation")
        row = wf.HumanReviewRequirement(
            requirement_id=requirement_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            cycle_id=cycle_id,
            route=route,
            question=question,
            baseline_content_version_id=baseline_content_version_id,
            state="open",
            opened_by_execution_id=execution_id,
            opened_at=opened_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_human_decision(
        self,
        *,
        decision_id: uuid.UUID,
        requirement_id: uuid.UUID,
        execution_id: uuid.UUID,
        decision: str,
        rationale: str,
        actor: str,
        decided_at: datetime,
    ) -> wf.HumanReviewDecision:
        requirement = self.session.get(wf.HumanReviewRequirement, requirement_id)
        execution = self.session.get(wf.CommandExecution, execution_id)
        if requirement is None or execution is None or requirement.state != "open":
            raise WorkflowAuthorityError("human review requirement is not open")
        row = wf.HumanReviewDecision(
            decision_id=decision_id,
            requirement_id=requirement_id,
            decision=decision,
            rationale=rationale,
            actor=actor,
            request_id=execution.request_id,
            command_execution_id=execution_id,
            decided_at=decided_at,
        )
        self.session.add(row)
        requirement.state = "decided"
        requirement.terminal_at = decided_at
        self.session.flush()
        return row

    def begin_abandonment(
        self,
        *,
        abandonment_id: uuid.UUID,
        execution_id: uuid.UUID,
        source_operation_id: uuid.UUID,
        source_lease_id: uuid.UUID,
        reason: str,
        created_at: datetime,
        source_cycle_id: uuid.UUID | None = None,
    ) -> wf.AbandonmentAttempt:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, source_operation_id)
        lease = self.session.get(wf.ServiceLease, source_lease_id)
        if execution is None or operation is None or lease is None:
            raise WorkflowAuthorityError("abandonment authority is incomplete")
        if lease.operation_id != source_operation_id or lease.task_id != operation.task_id:
            raise WorkflowAuthorityError("abandonment lease/operation mismatch")
        state = self.session.get(models.DishState, (execution.generation_id, operation.task_id))
        if state is None:
            raise WorkflowAuthorityError("abandonment requires exact task baseline")
        row = wf.AbandonmentAttempt(
            abandonment_id=abandonment_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            source_operation_id=source_operation_id,
            source_lease_id=source_lease_id,
            source_actor_attempt_sequence=lease.actor_attempt_sequence or 0,
            source_cycle_id=source_cycle_id,
            source_owner_id=lease.owner_id,
            source_run_id=lease.run_id,
            baseline_content_version_id=state.current_content_version_id,
            baseline_placement_version=state.placement_version,
            reason=reason,
            state="preparing",
            request_id=execution.request_id,
            command_execution_id=execution_id,
            successor_operation_id=None,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def mark_abandonment_blocked(
        self, *, abandonment_id: uuid.UUID, reason: str
    ) -> wf.AbandonmentAttempt:
        attempt = self.session.get(wf.AbandonmentAttempt, abandonment_id)
        if attempt is None or attempt.state not in {"preparing", "reconciling"}:
            raise WorkflowAuthorityError("abandonment cannot enter blocked state")
        attempt.state = "blocked"
        attempt.reason = reason
        self.session.flush()
        return attempt

    def repair_invocation_audit(
        self,
        *,
        obligation_id: uuid.UUID,
        repair_identity: str,
        source: str,
        payload: Mapping[str, Any],
        outcome: str,
        recorded_at: datetime,
        quarantine_reason: str | None = None,
    ) -> wf.InvocationAuditRepair:
        obligation = self.session.get(wf.InvocationAuditObligation, obligation_id)
        if obligation is None:
            raise WorkflowAuthorityError("unknown invocation-audit obligation")
        payload_dict = dict(payload)
        payload_sha = sha256_json(payload_dict)
        existing = self.session.scalar(
            select(wf.InvocationAuditRepair).where(
                wf.InvocationAuditRepair.repair_identity == repair_identity
            )
        )
        if existing is not None:
            if existing.payload_sha256 != payload_sha or existing.obligation_id != obligation_id:
                raise RequestIdentityConflict("repair identity conflict")
            return existing
        row = wf.InvocationAuditRepair(
            repair_id=self.uuid_factory(),
            obligation_id=obligation_id,
            repair_identity=repair_identity,
            source=source,
            payload=payload_dict,
            payload_sha256=payload_sha,
            outcome=outcome,
            quarantine_reason=quarantine_reason,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        obligation.state = outcome
        obligation.terminal_at = recorded_at
        self.session.flush()
        return row
