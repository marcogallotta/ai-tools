"""Stage 2 application services for foundational authority and import activation."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from .repositories import (
    AuthorityRepository,
    ContractBindingRepository,
    CoreAuthorityError,
    RegistryRepository,
    TaskRepository,
)


@dataclass(frozen=True)
class ImportedWorkflowOperationSpec:
    operation_id: uuid.UUID
    kind: str
    status: str
    phase: str
    terminal_outcome: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class ImportedServiceLeaseSpec:
    lease_id: uuid.UUID
    operation_id: uuid.UUID
    source_run_id: str
    owner_id: str
    lease_kind: str
    actor_attempt_sequence: int | None
    verification_cycle_id: uuid.UUID | None
    issued_at: datetime
    expires_at: datetime
    released_at: datetime | None


@dataclass(frozen=True)
class ImportedOperationRunRevocationSpec:
    revocation_id: uuid.UUID
    operation_id: uuid.UUID
    owner_id: str
    source_run_id: str
    source_lease_id: uuid.UUID | None
    reason: str
    revoked_at: datetime


@dataclass(frozen=True)
class ImportedVerificationCycleSpec:
    cycle_id: uuid.UUID
    operation_id: uuid.UUID
    cycle_sequence: int
    outcome: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class ImportedOperationHistorySpec:
    operations: tuple[ImportedWorkflowOperationSpec, ...] = ()
    leases: tuple[ImportedServiceLeaseSpec, ...] = ()
    verification_cycles: tuple[ImportedVerificationCycleSpec, ...] = ()
    revocations: tuple[ImportedOperationRunRevocationSpec, ...] = ()


@dataclass(frozen=True)
class ImportedTaskSpec:
    task_id: uuid.UUID
    asana_task_gid: str
    title: str
    body: str
    identity_scheme: str
    content_identity: str
    project_ids: tuple[uuid.UUID, ...]
    section_id: uuid.UUID
    completed: bool
    observed_at: datetime
    operation_history: ImportedOperationHistorySpec = field(
        default_factory=ImportedOperationHistorySpec
    )
    existence_state: str = "ordinary"


@dataclass(frozen=True)
class ImportedTaskResult:
    task_id: uuid.UUID
    content_version_id: uuid.UUID
    dish_version: int
    placement_version: int
    completion_version: int


@dataclass(frozen=True)
class OperationHistoryBackfillResult:
    matched_operations: int = 0
    inserted_operations: int = 0
    matched_verification_cycles: int = 0
    inserted_verification_cycles: int = 0
    matched_leases: int = 0
    inserted_leases: int = 0
    matched_revocations: int = 0
    inserted_revocations: int = 0

    @property
    def inserted_total(self) -> int:
        return (
            self.inserted_operations
            + self.inserted_verification_cycles
            + self.inserted_leases
            + self.inserted_revocations
        )


class CoreAuthorityService:
    """Orchestrate Stage 2 changes inside a caller-owned transaction."""

    def __init__(
        self,
        session: Session,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.authority = AuthorityRepository(session)
        self.contracts = ContractBindingRepository(session)
        self.registry = RegistryRepository(session)
        self.tasks = TaskRepository(session)

    def import_task_document(
        self,
        *,
        generation_id: uuid.UUID,
        import_run_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        spec: ImportedTaskSpec,
    ) -> ImportedTaskResult:
        """Create one complete import authority bundle without fake commands.

        The caller's transaction owns all-or-nothing durability. This method creates
        no service request, command execution, service run, or projection attempt.
        Historical workflow rows are admitted only with explicit import provenance.
        """

        generation = self.authority.generation(generation_id)
        if generation.status not in {"pending", "active"}:
            raise CoreAuthorityError("cannot import into a retired authority generation")
        import_run = self.session.get(models.ImportRun, import_run_id)
        if import_run is None or import_run.status != "complete":
            raise CoreAuthorityError("import authority requires a complete import run")
        binding = self.contracts.require(contract_binding_id)
        if binding.dish_release != generation.dish_release:
            raise CoreAuthorityError(
                "Honest contract binding does not match the generation Dish release"
            )
        active_registry, section = self.registry.require_registered_section(
            generation_id=generation_id, section_id=spec.section_id
        )
        if not spec.project_ids:
            raise CoreAuthorityError("imported task requires at least one governed project")
        if len(set(spec.project_ids)) != len(spec.project_ids):
            raise CoreAuthorityError("imported project memberships must be unique")
        if section.project_id not in spec.project_ids:
            raise CoreAuthorityError("section project must be an imported task membership")
        if not spec.asana_task_gid.isdigit() or spec.asana_task_gid.startswith("0"):
            raise CoreAuthorityError("Asana task alias must be a canonical positive decimal GID")

        task = models.DishTask(
            task_id=spec.task_id,
            existence_state=spec.existence_state,
            creation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            created_at=spec.observed_at,
            retired_at=None,
        )
        alias = models.TaskExternalAlias(
            alias_id=self.uuid_factory(),
            task_id=spec.task_id,
            external_system="asana",
            external_id=spec.asana_task_gid,
            origin="imported",
            import_run_id=import_run_id,
            projection_event_id=None,
            state="active",
            created_at=spec.observed_at,
            retired_at=None,
        )
        content_version_id = self.uuid_factory()
        version = models.ContentVersion(
            content_version_id=content_version_id,
            generation_id=generation_id,
            task_id=spec.task_id,
            representation_kind="document",
            title=spec.title,
            body=spec.body,
            identity_scheme=spec.identity_scheme,
            content_identity=spec.content_identity,
            creator_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            predecessor_content_version_id=None,
            contract_binding_id=contract_binding_id,
            created_dish_version=1,
            created_at=spec.observed_at,
        )
        receipt = models.DishMutationReceipt(
            generation_id=generation_id,
            task_id=spec.task_id,
            dish_version=1,
            source_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            content_changed=True,
            placement_changed=True,
            completion_changed=True,
            occurred_at=spec.observed_at,
        )
        state = models.DishState(
            generation_id=generation_id,
            task_id=spec.task_id,
            current_content_version_id=content_version_id,
            section_id=spec.section_id,
            registry_version_id=active_registry.registry_version_id,
            completed=spec.completed,
            completion_reason="imported",
            dish_version=1,
            placement_version=1,
            completion_version=1,
            updated_at=spec.observed_at,
        )
        membership_head = models.TaskMembershipHead(
            generation_id=generation_id,
            task_id=spec.task_id,
            membership_revision=1 if spec.project_ids else 0,
            updated_at=spec.observed_at,
        )

        membership_events: list[models.TaskProjectMembershipEvent] = []
        current_memberships: list[models.CurrentTaskProjectMembership] = []
        for project_id in spec.project_ids:
            project = self.session.get(models.GovernedProject, project_id)
            if project is None or project.lifecycle != "active":
                raise CoreAuthorityError("import membership requires active governed projects")
            event_id = self.uuid_factory()
            membership_events.append(
                models.TaskProjectMembershipEvent(
                    membership_event_id=event_id,
                    generation_id=generation_id,
                    task_id=spec.task_id,
                    project_id=project_id,
                    event_kind="joined",
                    membership_revision=1,
                    provenance_route="import",
                    import_run_id=import_run_id,
                    command_execution_id=None,
                    occurred_at=spec.observed_at,
                )
            )
            current_memberships.append(
                models.CurrentTaskProjectMembership(
                    generation_id=generation_id,
                    task_id=spec.task_id,
                    project_id=project_id,
                    latest_event_id=event_id,
                    is_member=True,
                    membership_revision=1,
                    updated_at=spec.observed_at,
                )
            )

        self.tasks.add_imported_task_bundle(
            task=task,
            alias=alias,
            receipt=receipt,
            version=version,
            state=state,
            membership_head=membership_head,
            membership_events=membership_events,
            current_memberships=current_memberships,
        )
        self.session.flush()
        self._import_operation_history(
            generation_id=generation_id,
            import_run_id=import_run_id,
            contract_binding_id=contract_binding_id,
            spec=spec,
        )
        return ImportedTaskResult(
            task_id=spec.task_id,
            content_version_id=content_version_id,
            dish_version=1,
            placement_version=1,
            completion_version=1,
        )

    @staticmethod
    def _operation_lifecycle(item: ImportedWorkflowOperationSpec) -> str:
        return (
            "completed"
            if item.status == "completed"
            else "abandoned"
            if item.terminal_outcome in {"agent_abandoned", "safe_reclaimed"}
            else "cancelled_by_marco"
        )

    @staticmethod
    def _verification_cycle_lifecycle(item: ImportedVerificationCycleSpec) -> str:
        return (
            item.outcome
            if item.outcome in {"approved", "rejected", "abandoned"}
            else "abandoned"
            if item.outcome == "safe_reclaimed"
            else "reset"
        )

    @staticmethod
    def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return left is right
        # SQLite drops timezone metadata for DateTime(timezone=True); production
        # PostgreSQL does not. Treat a naive stored value as UTC only for exact
        # source-value comparison so the same import semantics are testable on
        # both database boundaries.
        left_value = left if left.tzinfo is not None else left.replace(tzinfo=timezone.utc)
        right_value = right if right.tzinfo is not None else right.replace(tzinfo=timezone.utc)
        return left_value.timestamp() == right_value.timestamp()

    @staticmethod
    def _validate_operation_history(
        history: ImportedOperationHistorySpec,
    ) -> dict[uuid.UUID, ImportedVerificationCycleSpec]:
        operation_ids = {item.operation_id for item in history.operations}
        lease_ids = {item.lease_id for item in history.leases}
        cycle_ids = {item.cycle_id for item in history.verification_cycles}
        revocation_ids = {item.revocation_id for item in history.revocations}
        if len(operation_ids) != len(history.operations):
            raise CoreAuthorityError("imported operation history contains duplicate operation IDs")
        if len(lease_ids) != len(history.leases):
            raise CoreAuthorityError("imported operation history contains duplicate lease IDs")
        if len(cycle_ids) != len(history.verification_cycles):
            raise CoreAuthorityError(
                "imported operation history contains duplicate verification cycle IDs"
            )
        if len(revocation_ids) != len(history.revocations):
            raise CoreAuthorityError(
                "imported operation history contains duplicate revocation IDs"
            )
        exact_revocations = {
            (item.operation_id, item.owner_id, item.source_run_id) for item in history.revocations
        }
        if len(exact_revocations) != len(history.revocations):
            raise CoreAuthorityError(
                "imported operation history contains duplicate exact operation/run revocations"
            )

        for item in history.operations:
            if item.status not in {"completed", "cancelled"}:
                raise CoreAuthorityError(
                    "legacy operation-history import admits only terminal completed/cancelled operations"
                )
            if item.phase != "terminal" or item.completed_at is None or not item.terminal_outcome:
                raise CoreAuthorityError(
                    "imported terminal operation has inconsistent terminal evidence"
                )

        cycle_by_id = {item.cycle_id: item for item in history.verification_cycles}
        for item in history.verification_cycles:
            if item.operation_id not in operation_ids:
                raise CoreAuthorityError(
                    "imported verification cycle references another task/history"
                )
            if item.completed_at is None or item.outcome is None:
                raise CoreAuthorityError(
                    "legacy operation-history import does not admit open verification cycles"
                )
            if item.cycle_sequence <= 0:
                raise CoreAuthorityError(
                    "imported verification cycle sequence must be positive"
                )

        for item in history.leases:
            if item.operation_id not in operation_ids:
                raise CoreAuthorityError("imported lease references another task/history")
            if item.verification_cycle_id is not None:
                cycle = cycle_by_id.get(item.verification_cycle_id)
                if cycle is None or cycle.operation_id != item.operation_id:
                    raise CoreAuthorityError(
                        "imported lease verification cycle does not belong to its operation"
                    )
            if item.released_at is None:
                raise CoreAuthorityError(
                    "legacy operation-history import does not admit active service leases"
                )
            if not item.source_run_id.strip():
                raise CoreAuthorityError(
                    "imported lease source run identity must be nonblank"
                )
            if item.lease_kind == "actor":
                if item.actor_attempt_sequence is None or item.actor_attempt_sequence <= 0:
                    raise CoreAuthorityError(
                        "imported actor lease lacks a positive attempt sequence"
                    )
            elif item.lease_kind == "admin_request":
                if item.actor_attempt_sequence is not None:
                    raise CoreAuthorityError(
                        "imported admin lease carries actor attempt context"
                    )
            else:
                raise CoreAuthorityError(
                    f"unsupported imported lease kind: {item.lease_kind!r}"
                )

        lease_by_id = {item.lease_id: item for item in history.leases}
        for item in history.revocations:
            if item.operation_id not in operation_ids:
                raise CoreAuthorityError("imported revocation references another task/history")
            if not item.owner_id.strip() or not item.source_run_id.strip() or not item.reason.strip():
                raise CoreAuthorityError(
                    "imported revocation owner, source run, and reason must be nonblank"
                )
            if item.source_lease_id is not None:
                lease = lease_by_id.get(item.source_lease_id)
                if (
                    lease is None
                    or lease.operation_id != item.operation_id
                    or lease.owner_id != item.owner_id
                    or lease.source_run_id != item.source_run_id
                ):
                    raise CoreAuthorityError(
                        "imported revocation source lease does not prove its exact operation owner/run"
                    )
        return cycle_by_id

    def _new_imported_operation(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        import_run_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        item: ImportedWorkflowOperationSpec,
    ) -> wf.WorkflowOperation:
        return wf.WorkflowOperation(
            operation_id=item.operation_id,
            generation_id=generation_id,
            task_id=task_id,
            kind=item.kind,
            lifecycle=self._operation_lifecycle(item),
            phase="terminal",
            persisted_actions=[],
            import_run_id=import_run_id,
            creation_request_id=None,
            creation_execution_id=None,
            contract_binding_id=contract_binding_id,
            predecessor_operation_id=None,
            terminal_outcome=item.terminal_outcome,
            operation_revision=1,
            created_at=item.created_at,
            terminal_at=item.completed_at,
        )

    def _new_imported_cycle(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        import_run_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        item: ImportedVerificationCycleSpec,
    ) -> wf.VerificationCycle:
        return wf.VerificationCycle(
            cycle_id=item.cycle_id,
            generation_id=generation_id,
            task_id=task_id,
            operation_id=item.operation_id,
            reviewed_content_version_id=None,
            contract_binding_id=contract_binding_id,
            cycle_sequence=item.cycle_sequence,
            lifecycle=self._verification_cycle_lifecycle(item),
            outcome=item.outcome,
            import_run_id=import_run_id,
            created_by_execution_id=None,
            created_at=item.created_at,
            terminal_at=item.completed_at,
        )

    @staticmethod
    def _new_imported_lease(
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        import_run_id: uuid.UUID,
        item: ImportedServiceLeaseSpec,
    ) -> wf.ServiceLease:
        return wf.ServiceLease(
            lease_id=item.lease_id,
            generation_id=generation_id,
            task_id=task_id,
            operation_id=item.operation_id,
            run_id=None,
            import_run_id=import_run_id,
            source_run_id=item.source_run_id,
            owner_id=item.owner_id,
            lease_kind=item.lease_kind,
            actor_role=None,
            actor_attempt_sequence=item.actor_attempt_sequence,
            verification_cycle_id=item.verification_cycle_id,
            state="released",
            issued_at=item.issued_at,
            expires_at=item.expires_at,
            lease_revision=1,
            terminal_at=item.released_at,
        )

    @staticmethod
    def _new_imported_revocation(
        *,
        generation_id: uuid.UUID,
        import_run_id: uuid.UUID,
        item: ImportedOperationRunRevocationSpec,
    ) -> wf.OperationRunRevocation:
        return wf.OperationRunRevocation(
            revocation_id=item.revocation_id,
            generation_id=generation_id,
            operation_id=item.operation_id,
            owner_id=item.owner_id,
            run_id=None,
            import_run_id=import_run_id,
            source_run_id=item.source_run_id,
            source_lease_id=item.source_lease_id,
            reason=item.reason,
            revoked_at=item.revoked_at,
        )

    def _import_operation_history(
        self,
        *,
        generation_id: uuid.UUID,
        import_run_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        spec: ImportedTaskSpec,
    ) -> None:
        history = spec.operation_history
        if not (
            history.operations or history.leases or history.verification_cycles or history.revocations
        ):
            return
        self._validate_operation_history(history)
        for item in history.operations:
            self.session.add(
                self._new_imported_operation(
                    generation_id=generation_id,
                    task_id=spec.task_id,
                    import_run_id=import_run_id,
                    contract_binding_id=contract_binding_id,
                    item=item,
                )
            )
        self.session.flush()
        for item in history.verification_cycles:
            self.session.add(
                self._new_imported_cycle(
                    generation_id=generation_id,
                    task_id=spec.task_id,
                    import_run_id=import_run_id,
                    contract_binding_id=contract_binding_id,
                    item=item,
                )
            )
        self.session.flush()
        for item in history.leases:
            self.session.add(
                self._new_imported_lease(
                    generation_id=generation_id,
                    task_id=spec.task_id,
                    import_run_id=import_run_id,
                    item=item,
                )
            )
        self.session.flush()
        for item in history.revocations:
            self.session.add(
                self._new_imported_revocation(
                    generation_id=generation_id,
                    import_run_id=import_run_id,
                    item=item,
                )
            )

    def _require_complete_import_provenance(
        self, *, import_run_id: uuid.UUID | None, label: str
    ) -> None:
        if import_run_id is None:
            raise CoreAuthorityError(f"{label} stable identity already exists with live provenance")
        run = self.session.get(models.ImportRun, import_run_id)
        if run is None or run.status != "complete":
            raise CoreAuthorityError(
                f"{label} stable identity already exists with invalid import provenance"
            )

    def _operation_matches(
        self,
        row: wf.WorkflowOperation,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        item: ImportedWorkflowOperationSpec,
    ) -> bool:
        return (
            row.generation_id == generation_id
            and row.task_id == task_id
            and row.kind == item.kind
            and row.lifecycle == self._operation_lifecycle(item)
            and row.phase == "terminal"
            and row.persisted_actions == []
            and row.creation_request_id is None
            and row.creation_execution_id is None
            and row.contract_binding_id == contract_binding_id
            and row.predecessor_operation_id is None
            and row.terminal_outcome == item.terminal_outcome
            and row.operation_revision == 1
            and self._same_datetime(row.created_at, item.created_at)
            and self._same_datetime(row.terminal_at, item.completed_at)
        )

    def _cycle_matches(
        self,
        row: wf.VerificationCycle,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        item: ImportedVerificationCycleSpec,
    ) -> bool:
        return (
            row.generation_id == generation_id
            and row.task_id == task_id
            and row.operation_id == item.operation_id
            and row.reviewed_content_version_id is None
            and row.contract_binding_id == contract_binding_id
            and row.cycle_sequence == item.cycle_sequence
            and row.lifecycle == self._verification_cycle_lifecycle(item)
            and row.outcome == item.outcome
            and row.created_by_execution_id is None
            and self._same_datetime(row.created_at, item.created_at)
            and self._same_datetime(row.terminal_at, item.completed_at)
        )

    def _lease_matches(
        self,
        row: wf.ServiceLease,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        item: ImportedServiceLeaseSpec,
    ) -> bool:
        return (
            row.generation_id == generation_id
            and row.task_id == task_id
            and row.operation_id == item.operation_id
            and row.run_id is None
            and row.source_run_id == item.source_run_id
            and row.owner_id == item.owner_id
            and row.lease_kind == item.lease_kind
            and row.actor_role is None
            and row.actor_attempt_sequence == item.actor_attempt_sequence
            and row.verification_cycle_id == item.verification_cycle_id
            and row.state == "released"
            and row.lease_revision == 1
            and self._same_datetime(row.issued_at, item.issued_at)
            and self._same_datetime(row.expires_at, item.expires_at)
            and self._same_datetime(row.terminal_at, item.released_at)
        )

    def _revocation_matches(
        self,
        row: wf.OperationRunRevocation,
        *,
        generation_id: uuid.UUID,
        item: ImportedOperationRunRevocationSpec,
    ) -> bool:
        return (
            row.generation_id == generation_id
            and row.operation_id == item.operation_id
            and row.owner_id == item.owner_id
            and row.run_id is None
            and row.source_run_id == item.source_run_id
            and row.source_lease_id == item.source_lease_id
            and row.reason == item.reason
            and self._same_datetime(row.revoked_at, item.revoked_at)
        )

    def plan_operation_history_backfill(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        history: ImportedOperationHistorySpec,
    ) -> OperationHistoryBackfillResult:
        """Verify stable identities and report exactly which imported rows are missing."""
        generation = self.authority.generation(generation_id)
        if generation.status != "active":
            raise CoreAuthorityError("history backfill requires the active authority generation")
        task = self.session.get(models.DishTask, task_id)
        if task is None:
            raise CoreAuthorityError(f"history backfill requires existing DishTask: {task_id}")
        state = self.session.get(models.DishState, (generation_id, task_id))
        if state is None:
            raise CoreAuthorityError("history backfill task is absent from the active generation")
        binding = self.contracts.require(contract_binding_id)
        if binding.dish_release != generation.dish_release:
            raise CoreAuthorityError(
                "history backfill contract binding does not match the active generation"
            )
        self._validate_operation_history(history)

        matched_operations = inserted_operations = 0
        for item in history.operations:
            row = self.session.get(wf.WorkflowOperation, item.operation_id)
            if row is None:
                inserted_operations += 1
                continue
            self._require_complete_import_provenance(
                import_run_id=row.import_run_id, label=f"WorkflowOperation {item.operation_id}"
            )
            if not self._operation_matches(
                row,
                generation_id=generation_id,
                task_id=task_id,
                contract_binding_id=contract_binding_id,
                item=item,
            ):
                raise CoreAuthorityError(
                    f"WorkflowOperation stable identity conflicts with legacy history: {item.operation_id}"
                )
            matched_operations += 1

        matched_cycles = inserted_cycles = 0
        for item in history.verification_cycles:
            row = self.session.get(wf.VerificationCycle, item.cycle_id)
            if row is None:
                inserted_cycles += 1
                continue
            self._require_complete_import_provenance(
                import_run_id=row.import_run_id, label=f"VerificationCycle {item.cycle_id}"
            )
            if not self._cycle_matches(
                row,
                generation_id=generation_id,
                task_id=task_id,
                contract_binding_id=contract_binding_id,
                item=item,
            ):
                raise CoreAuthorityError(
                    f"VerificationCycle stable identity conflicts with legacy history: {item.cycle_id}"
                )
            matched_cycles += 1

        matched_leases = inserted_leases = 0
        for item in history.leases:
            row = self.session.get(wf.ServiceLease, item.lease_id)
            if row is None:
                inserted_leases += 1
                continue
            self._require_complete_import_provenance(
                import_run_id=row.import_run_id, label=f"ServiceLease {item.lease_id}"
            )
            if not self._lease_matches(
                row, generation_id=generation_id, task_id=task_id, item=item
            ):
                raise CoreAuthorityError(
                    f"ServiceLease stable identity conflicts with legacy history: {item.lease_id}"
                )
            matched_leases += 1

        matched_revocations = inserted_revocations = 0
        for item in history.revocations:
            row = self.session.get(wf.OperationRunRevocation, item.revocation_id)
            if row is None:
                inserted_revocations += 1
                continue
            self._require_complete_import_provenance(
                import_run_id=row.import_run_id,
                label=f"OperationRunRevocation {item.revocation_id}",
            )
            if not self._revocation_matches(
                row, generation_id=generation_id, item=item
            ):
                raise CoreAuthorityError(
                    "OperationRunRevocation stable identity conflicts with legacy history: "
                    f"{item.revocation_id}"
                )
            matched_revocations += 1

        return OperationHistoryBackfillResult(
            matched_operations=matched_operations,
            inserted_operations=inserted_operations,
            matched_verification_cycles=matched_cycles,
            inserted_verification_cycles=inserted_cycles,
            matched_leases=matched_leases,
            inserted_leases=inserted_leases,
            matched_revocations=matched_revocations,
            inserted_revocations=inserted_revocations,
        )

    def backfill_imported_operation_history(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        import_run_id: uuid.UUID,
        contract_binding_id: uuid.UUID,
        history: ImportedOperationHistorySpec,
    ) -> OperationHistoryBackfillResult:
        """Insert only missing terminal legacy history for an already imported task."""
        import_run = self.session.get(models.ImportRun, import_run_id)
        if import_run is None or import_run.status != "complete":
            raise CoreAuthorityError("history backfill requires a complete supplemental import run")
        provenance = import_run.provenance or {}
        if (
            provenance.get("import_kind") != "terminal-history-backfill-v1"
            or provenance.get("task_id") != str(task_id)
        ):
            raise CoreAuthorityError(
                "history backfill import run is not bound to this task as supplemental history"
            )

        plan = self.plan_operation_history_backfill(
            generation_id=generation_id,
            task_id=task_id,
            contract_binding_id=contract_binding_id,
            history=history,
        )
        for item in history.operations:
            if self.session.get(wf.WorkflowOperation, item.operation_id) is None:
                self.session.add(
                    self._new_imported_operation(
                        generation_id=generation_id,
                        task_id=task_id,
                        import_run_id=import_run_id,
                        contract_binding_id=contract_binding_id,
                        item=item,
                    )
                )
        self.session.flush()
        for item in history.verification_cycles:
            if self.session.get(wf.VerificationCycle, item.cycle_id) is None:
                self.session.add(
                    self._new_imported_cycle(
                        generation_id=generation_id,
                        task_id=task_id,
                        import_run_id=import_run_id,
                        contract_binding_id=contract_binding_id,
                        item=item,
                    )
                )
        self.session.flush()
        for item in history.leases:
            if self.session.get(wf.ServiceLease, item.lease_id) is None:
                self.session.add(
                    self._new_imported_lease(
                        generation_id=generation_id,
                        task_id=task_id,
                        import_run_id=import_run_id,
                        item=item,
                    )
                )
        self.session.flush()
        for item in history.revocations:
            if self.session.get(wf.OperationRunRevocation, item.revocation_id) is None:
                self.session.add(
                    self._new_imported_revocation(
                        generation_id=generation_id,
                        import_run_id=import_run_id,
                        item=item,
                    )
                )
        self.session.flush()
        return plan
