"""Stage 2 repositories.

Repositories participate in a caller-owned SQLAlchemy session. They flush to expose
constraint failures but never commit, rollback, or open nested transactions.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from . import models


class CoreAuthorityError(ValueError):
    """A requested Stage 2 authority transition is not structurally legal."""


class AuthorityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_import_run(self, row: models.ImportRun) -> None:
        self.session.add(row)
        self.session.flush()

    def add_generation(self, row: models.AuthorityGeneration) -> None:
        self.session.add(row)
        self.session.flush()

    def generation(self, generation_id: uuid.UUID) -> models.AuthorityGeneration:
        row = self.session.get(models.AuthorityGeneration, generation_id)
        if row is None:
            raise CoreAuthorityError(f"unknown authority generation: {generation_id}")
        return row

    def active_generation(self) -> models.AuthorityGeneration | None:
        return self.session.scalar(
            select(models.AuthorityGeneration).where(models.AuthorityGeneration.status == "active")
        )

    def activate_generation(
        self,
        *,
        generation_id: uuid.UUID,
        activation: models.AuthorityActivation,
        at: datetime,
    ) -> None:
        generation = self.generation(generation_id)
        if generation.status != "pending":
            raise CoreAuthorityError("only a pending generation may be activated")
        current = self.active_generation()
        if current is not None:
            if generation.predecessor_generation_id != current.generation_id:
                raise CoreAuthorityError("restore generation must name the current predecessor")
            current.status = "retired"
            current.retired_at = at
            self.session.flush()
        elif generation.creation_reason != "initial_cutover":
            raise CoreAuthorityError("the first active generation must be initial_cutover")
        generation.status = "active"
        self.session.add(activation)
        self.session.flush()

    def add_bootstrap_authority(self, row: models.GenerationBootstrapAuthority) -> None:
        self.session.add(row)
        self.session.flush()

    def add_migration_event(self, row: models.AppliedMigrationEvent) -> None:
        self.session.add(row)
        self.session.flush()


class ContractBindingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, row: models.HonestContractBinding) -> None:
        self.session.add(row)
        self.session.flush()

    def require(self, binding_id: uuid.UUID) -> models.HonestContractBinding:
        row = self.session.get(models.HonestContractBinding, binding_id)
        if row is None:
            raise CoreAuthorityError(f"unknown Honest contract binding: {binding_id}")
        return row


class RegistryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_project(self, row: models.GovernedProject) -> None:
        self.session.add(row)
        self.session.flush()

    def add_section(self, row: models.GovernedSection) -> None:
        project = self.session.get(models.GovernedProject, row.project_id)
        if project is None or project.lifecycle != "active":
            raise CoreAuthorityError("section requires an active governed project")
        self.session.add(row)
        self.session.flush()

    def add_project_alias(self, row: models.ProjectExternalAlias) -> None:
        self.session.add(row)
        self.session.flush()

    def add_section_alias(self, row: models.SectionExternalAlias) -> None:
        self.session.add(row)
        self.session.flush()

    def add_registry_version(
        self,
        row: models.SectionRegistryVersion,
        entries: Iterable[models.SectionRegistryEntry],
    ) -> None:
        generation = self.session.get(models.AuthorityGeneration, row.generation_id)
        binding = self.session.get(models.HonestContractBinding, row.contract_binding_id)
        import_run = self.session.get(models.ImportRun, row.import_run_id)
        if generation is None or generation.status == "retired":
            raise CoreAuthorityError(
                "registry version requires a non-retired authority generation"
            )
        if binding is None or binding.dish_release != generation.dish_release:
            raise CoreAuthorityError(
                "registry version requires a matching Honest contract binding"
            )
        if import_run is None or import_run.status != "complete":
            raise CoreAuthorityError("registry version requires a complete import run")
        self.session.add(row)
        self.session.flush()
        seen_roles: set[str] = set()
        seen_sections: set[uuid.UUID] = set()
        for entry in entries:
            if entry.registry_version_id != row.registry_version_id:
                raise CoreAuthorityError("registry entry belongs to a different version")
            if entry.workflow_role in seen_roles or entry.section_id in seen_sections:
                raise CoreAuthorityError("registry entries must have unique roles and sections")
            section = self.session.get(models.GovernedSection, entry.section_id)
            if section is None or section.lifecycle != "active":
                raise CoreAuthorityError("registry entry requires an active governed section")
            seen_roles.add(entry.workflow_role)
            seen_sections.add(entry.section_id)
            self.session.add(entry)
        if not seen_sections:
            raise CoreAuthorityError("registry version must contain at least one section")
        self.session.flush()

    def activate_registry(
        self,
        *,
        activation: models.SectionRegistryActivation,
        current: models.ActiveSectionRegistry,
    ) -> None:
        version = self.session.get(
            models.SectionRegistryVersion, activation.registry_version_id
        )
        if version is None or version.generation_id != activation.generation_id:
            raise CoreAuthorityError("registry activation generation/version mismatch")
        if current.generation_id != activation.generation_id:
            raise CoreAuthorityError("active registry generation mismatch")
        if current.registry_version_id != activation.registry_version_id:
            raise CoreAuthorityError("active registry version mismatch")
        if current.registry_activation_id != activation.registry_activation_id:
            raise CoreAuthorityError("active registry activation mismatch")
        if current.registry_revision != activation.registry_revision:
            raise CoreAuthorityError("active registry revision mismatch")
        existing = self.session.get(models.ActiveSectionRegistry, current.generation_id)
        self.session.add(activation)
        self.session.flush()
        if existing is None:
            self.session.add(current)
        else:
            existing.registry_version_id = current.registry_version_id
            existing.registry_activation_id = current.registry_activation_id
            existing.registry_revision = current.registry_revision
            existing.updated_at = current.updated_at
        self.session.flush()

    def active_registry(self, generation_id: uuid.UUID) -> models.ActiveSectionRegistry:
        row = self.session.get(models.ActiveSectionRegistry, generation_id)
        if row is None:
            raise CoreAuthorityError("generation has no active section registry")
        return row

    def require_registered_section(
        self,
        *,
        generation_id: uuid.UUID,
        section_id: uuid.UUID,
    ) -> tuple[models.ActiveSectionRegistry, models.GovernedSection]:
        active = self.active_registry(generation_id)
        entry = self.session.get(
            models.SectionRegistryEntry,
            (active.registry_version_id, section_id),
        )
        if entry is None:
            raise CoreAuthorityError("section is not present in the active registry")
        section = self.session.get(models.GovernedSection, section_id)
        if section is None or section.lifecycle != "active":
            raise CoreAuthorityError("active registry points to an inactive section")
        return active, section


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_imported_task_bundle(
        self,
        *,
        task: models.DishTask,
        alias: models.TaskExternalAlias,
        version: models.ContentVersion,
        activation: models.ContentActivation,
        head: models.TaskAuthorityHead,
        membership_events: Iterable[models.TaskProjectMembershipEvent],
        current_memberships: Iterable[models.CurrentTaskProjectMembership],
        placement_event: models.TaskSectionPlacementEvent,
        current_placement: models.CurrentTaskSectionPlacement,
        completion_event: models.TaskCompletionEvent,
        current_completion: models.CurrentTaskCompletion,
    ) -> None:
        if task.creation_route != "import":
            raise CoreAuthorityError("Stage 2 import bundle requires import creation provenance")
        if alias.task_id != task.task_id or alias.origin != "imported":
            raise CoreAuthorityError("task alias does not match imported task")
        if version.task_id != task.task_id or activation.task_id != task.task_id:
            raise CoreAuthorityError("content authority does not match imported task")
        if activation.content_version_id != version.content_version_id:
            raise CoreAuthorityError("content activation does not target imported version")
        if head.current_content_activation_id != activation.content_activation_id:
            raise CoreAuthorityError("task head does not target imported activation")
        if head.task_id != task.task_id or head.generation_id != version.generation_id:
            raise CoreAuthorityError("task head generation/task mismatch")

        self.session.add(task)
        self.session.flush()
        self.session.add_all([alias, version])
        self.session.flush()
        self.session.add(activation)
        self.session.flush()
        self.session.add(head)
        self.session.flush()

        membership_rows = list(membership_events)
        current_rows = list(current_memberships)
        if len(membership_rows) != len(current_rows):
            raise CoreAuthorityError("each imported membership needs one current row")
        self.session.add_all(membership_rows)
        self.session.flush()
        self.session.add_all(current_rows)
        self.session.add(placement_event)
        self.session.add(completion_event)
        self.session.flush()
        self.session.add(current_placement)
        self.session.add(current_completion)
        self.session.flush()

    def task_head(
        self, *, generation_id: uuid.UUID, task_id: uuid.UUID
    ) -> models.TaskAuthorityHead | None:
        return self.session.get(models.TaskAuthorityHead, (generation_id, task_id))

    def task_by_external_alias(
        self, *, external_system: str, external_id: str
    ) -> models.DishTask | None:
        statement: Select[tuple[models.DishTask]] = (
            select(models.DishTask)
            .join(
                models.TaskExternalAlias,
                models.TaskExternalAlias.task_id == models.DishTask.task_id,
            )
            .where(
                models.TaskExternalAlias.external_system == external_system,
                models.TaskExternalAlias.external_id == external_id,
                models.TaskExternalAlias.state == "active",
            )
        )
        return self.session.scalar(statement)
