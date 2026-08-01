"""Stage 2 application services for foundational authority and import activation."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy.orm import Session

from . import models
from .repositories import (
    AuthorityRepository,
    ContractBindingRepository,
    CoreAuthorityError,
    RegistryRepository,
    TaskRepository,
)


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
    existence_state: str = "ordinary"


@dataclass(frozen=True)
class ImportedTaskResult:
    task_id: uuid.UUID
    content_version_id: uuid.UUID
    content_activation_id: uuid.UUID
    placement_event_id: uuid.UUID
    completion_event_id: uuid.UUID


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
        no service request, command execution, workflow operation, or projection
        attempt. Exact import provenance is present on every imported authority row.
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
            created_at=spec.observed_at,
        )
        content_activation_id = self.uuid_factory()
        activation = models.ContentActivation(
            content_activation_id=content_activation_id,
            generation_id=generation_id,
            task_id=spec.task_id,
            content_version_id=content_version_id,
            activation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            task_revision=1,
            activated_at=spec.observed_at,
        )
        head = models.TaskAuthorityHead(
            generation_id=generation_id,
            task_id=spec.task_id,
            current_content_activation_id=content_activation_id,
            task_revision=1,
            membership_revision=1,
            placement_revision=1,
            completion_revision=1,
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

        placement_event_id = self.uuid_factory()
        placement_event = models.TaskSectionPlacementEvent(
            placement_event_id=placement_event_id,
            generation_id=generation_id,
            task_id=spec.task_id,
            section_id=spec.section_id,
            registry_version_id=active_registry.registry_version_id,
            event_kind="placed",
            placement_revision=1,
            provenance_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            occurred_at=spec.observed_at,
        )
        current_placement = models.CurrentTaskSectionPlacement(
            generation_id=generation_id,
            task_id=spec.task_id,
            section_id=spec.section_id,
            registry_version_id=active_registry.registry_version_id,
            latest_event_id=placement_event_id,
            placement_revision=1,
            updated_at=spec.observed_at,
        )

        completion_event_id = self.uuid_factory()
        completion_event = models.TaskCompletionEvent(
            completion_event_id=completion_event_id,
            generation_id=generation_id,
            task_id=spec.task_id,
            completed=spec.completed,
            reason="imported",
            completion_revision=1,
            provenance_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            occurred_at=spec.observed_at,
        )
        current_completion = models.CurrentTaskCompletion(
            generation_id=generation_id,
            task_id=spec.task_id,
            completed=spec.completed,
            latest_event_id=completion_event_id,
            completion_revision=1,
            updated_at=spec.observed_at,
        )

        self.tasks.add_imported_task_bundle(
            task=task,
            alias=alias,
            version=version,
            activation=activation,
            head=head,
            membership_events=membership_events,
            current_memberships=current_memberships,
            placement_event=placement_event,
            current_placement=current_placement,
            completion_event=completion_event,
            current_completion=current_completion,
        )
        return ImportedTaskResult(
            task_id=spec.task_id,
            content_version_id=content_version_id,
            content_activation_id=content_activation_id,
            placement_event_id=placement_event_id,
            completion_event_id=completion_event_id,
        )
