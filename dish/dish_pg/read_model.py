"""Stage 4 PostgreSQL authoritative reads and current-view construction."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions

from . import models
from . import stage3_models as wf
from .document_authority import CanonicalDocumentError, parse_canonical_document
from .repositories import ActiveCatalogContract, CatalogRepository, CoreAuthorityError


class ReadModelError(ValueError):
    """The requested authoritative read cannot be satisfied safely."""


class InvalidCursor(ReadModelError):
    """A pagination cursor is malformed, stale, or bound to another query."""


@dataclass(frozen=True)
class TaskListItem:
    task_id: uuid.UUID
    title: str
    completed: bool
    section_id: uuid.UUID
    external_task_id: str | None


@dataclass(frozen=True)
class TaskListPage:
    items: tuple[TaskListItem, ...]
    next_cursor: str | None
    read_authority: Mapping[str, Any]


@dataclass(frozen=True)
class _NativeReadAuthority:
    pointer: models.CurrentNativeCatalogRuntime
    attestation: models.NativeCatalogRuntimeAttestation
    catalog: ActiveCatalogContract

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "catalog_version_id": str(self.pointer.catalog_version_id),
            "catalog_activation_id": str(self.pointer.catalog_activation_id),
            "catalog_revision": self.catalog.active_catalog.catalog_revision,
            "runtime_attestation_id": str(self.pointer.attestation_id),
            "runtime_attestation_revision": self.pointer.attestation_revision,
        }


@dataclass(frozen=True)
class TaskCurrentView:
    task_id: uuid.UUID
    title: str
    body: str
    content_version_id: uuid.UUID
    dish_version: int
    task_revision: int
    membership_revision: int | None
    placement_revision: int
    completion_revision: int
    section_id: uuid.UUID
    completed: bool
    completion_reason: str
    archived_at: datetime | None
    completion_state: str
    operation_id: uuid.UUID | None
    operation_phase: str | None
    operation_revision: int | None
    legal_actions: tuple[str, ...]
    projection_freshness: Mapping[str, Any]
    read_authority: Mapping[str, Any]


class CursorCodec:
    """Authenticated opaque cursors; cursors are read artifacts, never authority."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("cursor secret must contain at least 32 bytes")
        self.secret = secret

    def encode(self, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw + sig).decode("ascii").rstrip("=")

    def decode(self, token: str) -> dict[str, Any]:
        try:
            padded = token + "=" * (-len(token) % 4)
            value = base64.urlsafe_b64decode(padded.encode("ascii"))
            raw, supplied = value[:-32], value[-32:]
            expected = hmac.new(self.secret, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                raise InvalidCursor("cursor authentication failed")
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidCursor("invalid opaque cursor") from exc
        if not isinstance(decoded, dict):
            raise InvalidCursor("invalid opaque cursor payload")
        return decoded


def _status_from_document(title: str, body: str) -> str | None:
    try:
        parts = parse_canonical_document(title=title, body=body)
    except CanonicalDocumentError:
        return None
    return parts.document.state.values["Status"]


class PostgresReadModel:
    """Consistent reads over one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session, *, cursor_secret: bytes) -> None:
        self.session = session
        self.cursor_codec = CursorCodec(cursor_secret)

    def active_generation(self) -> models.AuthorityGeneration:
        row = self.session.scalar(
            select(models.AuthorityGeneration).where(models.AuthorityGeneration.status == "active")
        )
        if row is None:
            raise ReadModelError("no active authority generation")
        return row

    def _active_registry(self, generation_id: uuid.UUID) -> models.ActiveSectionRegistry:
        row = self.session.get(models.ActiveSectionRegistry, generation_id)
        if row is None:
            raise ReadModelError("active generation has no section registry")
        return row

    def _native_read_authority(
        self, generation_id: uuid.UUID
    ) -> _NativeReadAuthority | None:
        try:
            resolved = models.resolve_current_native_catalog_runtime(
                self.session, generation_id
            )
        except ValueError as exc:
            raise ReadModelError("native runtime current pointer is inconsistent") from exc
        if resolved is None:
            transition_recorded = any(
                event.details.get("authority_transition")
                == "native_section_runtime_root_v1"
                for event in self.session.scalars(
                    select(models.AppliedMigrationEvent).where(
                        models.AppliedMigrationEvent.generation_id == generation_id,
                        models.AppliedMigrationEvent.outcome == "applied",
                    )
                )
            )
            if transition_recorded:
                raise ReadModelError(
                    "native runtime root is missing after the authority transition"
                )
            return None

        pointer, current_attestation = resolved
        try:
            catalog = CatalogRepository(self.session).active_catalog_contract(generation_id)
        except CoreAuthorityError as exc:
            raise ReadModelError(str(exc)) from exc
        if (
            pointer.catalog_version_id != catalog.active_catalog.catalog_version_id
            or pointer.catalog_activation_id
            != catalog.active_catalog.catalog_activation_id
        ):
            raise ReadModelError("native runtime pointer is stale against the active catalog")

        attestations = tuple(
            self.session.scalars(
                select(models.NativeCatalogRuntimeAttestation)
                .where(
                    models.NativeCatalogRuntimeAttestation.generation_id
                    == generation_id
                )
                .order_by(models.NativeCatalogRuntimeAttestation.attestation_revision)
            )
        )
        expected_revisions = tuple(range(1, pointer.attestation_revision + 1))
        if (
            tuple(row.attestation_revision for row in attestations)
            != expected_revisions
            or not attestations
            or attestations[-1].attestation_id != pointer.attestation_id
        ):
            raise ReadModelError("native runtime attestation lineage is gapped or stale")

        predecessor_id: uuid.UUID | None = None
        for attestation in attestations:
            if attestation.predecessor_attestation_id != predecessor_id:
                raise ReadModelError("native runtime attestation lineage is forked")
            version = self.session.get(
                models.SectionCatalogVersion, attestation.catalog_version_id
            )
            activation = self.session.get(
                models.SectionCatalogActivation, attestation.catalog_activation_id
            )
            if (
                version is None
                or activation is None
                or version.generation_id != generation_id
                or activation.generation_id != generation_id
                or activation.catalog_version_id != version.catalog_version_id
            ):
                raise ReadModelError("native runtime attestation catalog identity is inconsistent")

            hash_fields: dict[str, Any] = {}
            if attestation.attestation_revision == 1:
                event = self.session.get(
                    models.AppliedMigrationEvent,
                    attestation.baseline_migration_event_id,
                )
                details = event.details if event is not None else {}
                source_commit_sha = details.get("source_commit_sha")
                if (
                    event is None
                    or event.generation_id != generation_id
                    or event.outcome != "applied"
                    or details.get("authority_transition")
                    != "native_section_runtime_root_v1"
                    or details.get("catalog_activation_id")
                    != str(attestation.catalog_activation_id)
                    or details.get("catalog_version_id")
                    != str(attestation.catalog_version_id)
                    or details.get("honest_contract_binding_id")
                    != str(version.contract_binding_id)
                    or not isinstance(source_commit_sha, str)
                    or not source_commit_sha.strip()
                ):
                    raise ReadModelError("native runtime root migration witness is inconsistent")
                hash_fields = {
                    "baseline_revision": event.revision,
                    "baseline_migration_code_sha256": event.migration_code_sha256,
                    "baseline_dish_release": event.dish_release,
                    "baseline_source_commit_sha": source_commit_sha,
                }
            expected_hash = models.compute_attestation_sha256(
                generation_id=generation_id,
                catalog_version_id=attestation.catalog_version_id,
                catalog_activation_id=attestation.catalog_activation_id,
                contract_binding_id=version.contract_binding_id,
                attestation_revision=attestation.attestation_revision,
                predecessor_attestation_id=attestation.predecessor_attestation_id,
                baseline_migration_event_id=attestation.baseline_migration_event_id,
                **hash_fields,
            )
            if expected_hash != attestation.attestation_sha256:
                raise ReadModelError("native runtime attestation hash is inconsistent")
            predecessor_id = attestation.attestation_id

        return _NativeReadAuthority(
            pointer=pointer,
            attestation=current_attestation,
            catalog=catalog,
        )

    def sections(self) -> tuple[dict[str, Any], ...]:
        generation = self.active_generation()
        native = self._native_read_authority(generation.generation_id)
        if native is not None:
            rows = self.session.execute(
                select(
                    models.SectionCatalogEntry,
                    models.Section,
                    models.SectionExternalAlias.external_id,
                )
                .join(
                    models.Section,
                    models.Section.section_id == models.SectionCatalogEntry.section_id,
                )
                .outerjoin(
                    models.SectionExternalAlias,
                    and_(
                        models.SectionExternalAlias.section_id == models.Section.section_id,
                        models.SectionExternalAlias.external_system == "asana",
                        models.SectionExternalAlias.state == "active",
                    ),
                )
                .where(
                    models.SectionCatalogEntry.catalog_version_id
                    == native.pointer.catalog_version_id
                )
                .order_by(models.SectionCatalogEntry.ordinal)
            ).all()
            return tuple(
                {
                    "section_id": str(entry.section_id),
                    "section_gid": external_id,
                    "name": entry.display_name,
                    "workflow_role": entry.workflow_role,
                    "ordinal": entry.ordinal,
                    **native.identity,
                }
                for entry, _section, external_id in rows
            )
        active = self._active_registry(generation.generation_id)
        rows = self.session.execute(
            select(
                models.SectionRegistryEntry,
                models.GovernedSection,
                models.SectionExternalAlias.external_id,
            )
            .join(
                models.GovernedSection,
                models.GovernedSection.section_id == models.SectionRegistryEntry.section_id,
            )
            .outerjoin(
                models.SectionExternalAlias,
                and_(
                    models.SectionExternalAlias.section_id == models.GovernedSection.section_id,
                    models.SectionExternalAlias.external_system == "asana",
                    models.SectionExternalAlias.state == "active",
                ),
            )
            .where(models.SectionRegistryEntry.registry_version_id == active.registry_version_id)
            .order_by(models.SectionRegistryEntry.ordinal)
        ).all()
        return tuple(
            {
                "section_id": str(entry.section_id),
                "section_gid": external_id,
                "name": entry.display_name,
                "workflow_role": entry.workflow_role,
                "ordinal": entry.ordinal,
                "registry_version_id": str(active.registry_version_id),
                "registry_revision": active.registry_revision,
            }
            for entry, _section, external_id in rows
        )

    def _resolve_legacy_section(
        self, reference: str | uuid.UUID
    ) -> models.GovernedSection:
        if isinstance(reference, uuid.UUID):
            row = self.session.get(models.GovernedSection, reference)
        else:
            try:
                parsed = uuid.UUID(reference)
            except ValueError:
                parsed = None
            row = self.session.get(models.GovernedSection, parsed) if parsed else None
            if row is None:
                row = self.session.scalar(
                    select(models.GovernedSection)
                    .join(
                        models.SectionExternalAlias,
                        models.SectionExternalAlias.section_id == models.GovernedSection.section_id,
                    )
                    .where(
                        models.SectionExternalAlias.external_system == "asana",
                        models.SectionExternalAlias.external_id == reference,
                        models.SectionExternalAlias.state == "active",
                    )
                )
        if row is None:
            raise ReadModelError("unknown governed section")
        return row

    def _resolve_native_section(
        self,
        reference: str | uuid.UUID,
        native: _NativeReadAuthority,
    ) -> models.Section:
        if isinstance(reference, uuid.UUID):
            section_id = reference
        else:
            try:
                section_id = uuid.UUID(reference)
            except ValueError:
                section_id = self.session.scalar(
                    select(models.SectionExternalAlias.section_id).where(
                        models.SectionExternalAlias.external_system == "asana",
                        models.SectionExternalAlias.external_id == reference,
                        models.SectionExternalAlias.state == "active",
                    )
                )
        section = self.session.get(models.Section, section_id) if section_id else None
        if section is None or section.lifecycle != "active":
            raise ReadModelError("unknown active native Section")
        catalog_entry = self.session.scalar(
            select(models.SectionCatalogEntry).where(
                models.SectionCatalogEntry.catalog_version_id
                == native.pointer.catalog_version_id,
                models.SectionCatalogEntry.section_id == section.section_id,
            )
        )
        if catalog_entry is None:
            raise ReadModelError("Section is not in the active native catalog")
        return section

    def resolve_section(
        self, reference: str | uuid.UUID
    ) -> models.GovernedSection | models.Section:
        generation = self.active_generation()
        native = self._native_read_authority(generation.generation_id)
        if native is None:
            return self._resolve_legacy_section(reference)
        return self._resolve_native_section(reference, native)

    def resolve_task(self, reference: str | uuid.UUID) -> models.DishTask:
        if isinstance(reference, uuid.UUID):
            row = self.session.get(models.DishTask, reference)
        else:
            try:
                parsed = uuid.UUID(reference)
            except ValueError:
                parsed = None
            row = self.session.get(models.DishTask, parsed) if parsed else None
            if row is None:
                row = self.session.scalar(
                    select(models.DishTask)
                    .join(
                        models.TaskExternalAlias,
                        models.TaskExternalAlias.task_id == models.DishTask.task_id,
                    )
                    .where(
                        models.TaskExternalAlias.external_system == "asana",
                        models.TaskExternalAlias.external_id == reference,
                        models.TaskExternalAlias.state == "active",
                    )
                )
        if row is None or row.existence_state == "retired":
            raise ReadModelError("unknown active Dish task")
        return row

    def section_tasks(
        self,
        *,
        section_reference: str | uuid.UUID,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> TaskListPage:
        if not 1 <= page_size <= 100:
            raise ReadModelError("page_size must be between 1 and 100")
        generation = self.active_generation()
        native = self._native_read_authority(generation.generation_id)
        if native is None:
            active = self._active_registry(generation.generation_id)
            section = self._resolve_legacy_section(section_reference)
            registered = self.session.scalar(
                select(models.SectionRegistryEntry).where(
                    models.SectionRegistryEntry.registry_version_id == active.registry_version_id,
                    models.SectionRegistryEntry.section_id == section.section_id,
                )
            )
            if registered is None:
                raise ReadModelError("section is not in the active registry")
            read_authority = {
                "registry_version_id": str(active.registry_version_id),
                "registry_revision": active.registry_revision,
            }
            placement_currentness = (
                models.DishState.registry_version_id == active.registry_version_id
            )
        else:
            section = self._resolve_native_section(section_reference, native)
            read_authority = native.identity
            placement_currentness = (
                models.DishState.catalog_version_id == native.pointer.catalog_version_id
            )

        after_title: str | None = None
        after_task: uuid.UUID | None = None
        if cursor:
            payload = self.cursor_codec.decode(cursor)
            expected = {
                "generation_id": str(generation.generation_id),
                **read_authority,
                "section_id": str(section.section_id),
                "page_size": page_size,
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise InvalidCursor("cursor is stale or belongs to another list query")
            try:
                after_title = str(payload["after_title"])
                after_task = uuid.UUID(str(payload["after_task_id"]))
            except (KeyError, ValueError) as exc:
                raise InvalidCursor("cursor page boundary is invalid") from exc

        title_key = func.lower(models.ContentVersion.title)
        statement = (
            select(
                models.DishTask.task_id,
                models.ContentVersion.title,
                models.DishState.completed,
                models.TaskExternalAlias.external_id,
            )
            .join(
                models.DishState,
                and_(
                    models.DishState.generation_id == generation.generation_id,
                    models.DishState.task_id == models.DishTask.task_id,
                ),
            )
            .join(
                models.ContentVersion,
                models.ContentVersion.content_version_id
                == models.DishState.current_content_version_id,
            )
            .outerjoin(
                models.TaskExternalAlias,
                and_(
                    models.TaskExternalAlias.task_id == models.DishTask.task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                ),
            )
            .where(
                models.DishState.section_id == section.section_id,
                placement_currentness,
                models.DishState.completed.is_(False),
                models.DishState.archived_at.is_(None),
                models.DishTask.existence_state != "retired",
            )
            .order_by(title_key, models.DishTask.task_id)
            .limit(page_size + 1)
        )
        if after_title is not None and after_task is not None:
            statement = statement.where(
                or_(
                    title_key > after_title,
                    and_(title_key == after_title, models.DishTask.task_id > after_task),
                )
            )
        rows = self.session.execute(statement).all()
        visible = rows[:page_size]
        items = tuple(
            TaskListItem(
                task_id=task_id,
                title=title,
                completed=completed,
                section_id=section.section_id,
                external_task_id=external_id,
            )
            for task_id, title, completed, external_id in visible
        )
        next_cursor = None
        if len(rows) > page_size and visible:
            last = visible[-1]
            next_cursor = self.cursor_codec.encode(
                {
                    "generation_id": str(generation.generation_id),
                    **read_authority,
                    "section_id": str(section.section_id),
                    "page_size": page_size,
                    "after_title": last[1].lower(),
                    "after_task_id": str(last[0]),
                }
            )
        return TaskListPage(
            items=items,
            next_cursor=next_cursor,
            read_authority=read_authority,
        )

    def native_search(
        self, *, query: str, page_size: int, cursor: str | None
    ) -> Mapping[str, Any] | None:
        generation = self.active_generation()
        native = self._native_read_authority(generation.generation_id)
        if native is None:
            return None
        identity = {
            "kind": "active-title-search-v1",
            "generation_id": str(generation.generation_id),
            **native.identity,
            "query": query.lower(),
            "page_size": page_size,
        }
        offset = 0
        if cursor is not None:
            payload = self.cursor_codec.decode(cursor)
            if any(payload.get(key) != value for key, value in identity.items()):
                raise InvalidCursor("cursor is stale or belongs to another search query")
            try:
                offset = int(payload["offset"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor("cursor page boundary is invalid") from exc
            if offset < 0:
                raise InvalidCursor("cursor page boundary is invalid")
        rows = list(
            self.session.execute(
                select(
                    models.DishTask.task_id, models.ContentVersion.title,
                    models.DishState.section_id, models.SectionCatalogEntry.display_name,
                    models.SectionCatalogEntry.workflow_role, models.GovernedProject.logical_name,
                )
                .select_from(models.DishTask)
                .join(models.DishState, and_(
                    models.DishState.generation_id == generation.generation_id,
                    models.DishState.task_id == models.DishTask.task_id,
                ))
                .join(models.ContentVersion, and_(
                    models.ContentVersion.generation_id == generation.generation_id,
                    models.ContentVersion.task_id == models.DishTask.task_id,
                    models.ContentVersion.content_version_id == models.DishState.current_content_version_id,
                ))
                .join(models.SectionCatalogEntry, and_(
                    models.SectionCatalogEntry.catalog_version_id == native.pointer.catalog_version_id,
                    models.SectionCatalogEntry.section_id == models.DishState.section_id,
                ))
                .outerjoin(models.GovernedSection, models.GovernedSection.section_id == models.DishState.section_id)
                .outerjoin(models.GovernedProject, models.GovernedProject.project_id == models.GovernedSection.project_id)
                .where(
                    models.DishState.catalog_version_id == native.pointer.catalog_version_id,
                    models.DishState.completed.is_(False), models.DishState.archived_at.is_(None),
                    models.DishTask.existence_state.in_(("ordinary", "isolated")),
                    func.lower(models.ContentVersion.title).contains(query.lower(), autoescape=True),
                )
                .order_by(func.lower(models.ContentVersion.title), models.DishTask.task_id)
                .offset(offset).limit(page_size + 1)
            )
        )
        visible = rows[:page_size]
        results = []
        for task_id, title, section_id, section_label, workflow_role, project_label in visible:
            task_gid = self.session.scalar(select(models.TaskExternalAlias.external_id).where(
                models.TaskExternalAlias.task_id == task_id,
                models.TaskExternalAlias.external_system == "asana",
                models.TaskExternalAlias.state == "active",
            ))
            results.append({
                "dish_id": str(task_id), "title": title, "section_id": str(section_id),
                "section_label": section_label, "workflow_role": workflow_role,
                "project_label": project_label, **({"task_gid": task_gid} if task_gid else {}),
            })
        next_cursor = self.cursor_codec.encode(identity | {"offset": offset + page_size}) if len(rows) > page_size else None
        return {"query": query, "results": results, "next_cursor": next_cursor,
                "page_size": page_size, "generation_id": str(generation.generation_id), **native.identity}

    def _workflow_snapshot(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        title: str,
        body: str,
        operation: wf.WorkflowOperation,
    ) -> WorkflowSnapshot:
        placement = self.session.get(models.DishState, (generation_id, task_id))
        live_section_gid = None
        if placement is not None:
            live_section_gid = self.session.scalar(
                select(models.SectionExternalAlias.external_id).where(
                    models.SectionExternalAlias.section_id == placement.section_id,
                    models.SectionExternalAlias.external_system == "asana",
                    models.SectionExternalAlias.state == "active",
                )
            )
        active = self._active_registry(generation_id)
        verification_section = self.session.scalar(
            select(models.SectionRegistryEntry.section_id).where(
                models.SectionRegistryEntry.registry_version_id == active.registry_version_id,
                models.SectionRegistryEntry.workflow_role == "verification_queue",
            )
        )
        verification_gid = None
        if verification_section is not None:
            verification_gid = self.session.scalar(
                select(models.SectionExternalAlias.external_id).where(
                    models.SectionExternalAlias.section_id == verification_section,
                    models.SectionExternalAlias.external_system == "asana",
                    models.SectionExternalAlias.state == "active",
                )
            )
        cycle = self.session.scalar(
            select(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation.operation_id)
            .order_by(wf.VerificationCycle.cycle_sequence.desc())
            .limit(1)
        )
        verifier_established = False
        inspected = False
        signoff_bound = False
        if cycle is not None:
            verifier_established = self.session.scalar(
                select(func.count())
                .select_from(wf.OperationActorFact)
                .join(
                    wf.ServiceLease,
                    (wf.ServiceLease.operation_id == wf.OperationActorFact.operation_id)
                    & (wf.ServiceLease.run_id == wf.OperationActorFact.run_id)
                    & (wf.ServiceLease.owner_id == wf.OperationActorFact.owner_id)
                    & (wf.ServiceLease.actor_role == wf.OperationActorFact.actor_role)
                    & (
                        wf.ServiceLease.actor_attempt_sequence
                        == wf.OperationActorFact.actor_attempt_sequence
                    ),
                )
                .where(
                    wf.OperationActorFact.operation_id == operation.operation_id,
                    wf.OperationActorFact.task_id == task_id,
                    wf.OperationActorFact.actor_role == "verification",
                    wf.ServiceLease.generation_id == generation_id,
                    wf.ServiceLease.task_id == task_id,
                    wf.ServiceLease.verification_cycle_id == cycle.cycle_id,
                )
            ) > 0
            inspected = self.session.scalar(
                select(func.count())
                .select_from(wf.VerificationInspectionOccurrence)
                .where(wf.VerificationInspectionOccurrence.cycle_id == cycle.cycle_id)
            ) > 0
            signoff_bound = self.session.scalar(
                select(func.count())
                .select_from(wf.VerificationSignoff)
                .where(wf.VerificationSignoff.cycle_id == cycle.cycle_id)
            ) > 0
        latest_hold = self.session.scalar(
            select(wf.EvidenceHold)
            .where(wf.EvidenceHold.operation_id == operation.operation_id)
            .order_by(wf.EvidenceHold.opened_at.desc(), wf.EvidenceHold.hold_id.desc())
            .limit(1)
        )
        latest_human = self.session.scalar(
            select(wf.HumanReviewRequirement)
            .where(wf.HumanReviewRequirement.operation_id == operation.operation_id)
            .order_by(
                wf.HumanReviewRequirement.opened_at.desc(),
                wf.HumanReviewRequirement.requirement_id.desc(),
            )
            .limit(1)
        )
        active_hold = (
            latest_hold
            if latest_hold is not None and latest_hold.state == "open"
            else None
        )
        active_human = (
            latest_human
            if latest_human is not None and latest_human.state == "open"
            else None
        )
        latest_route = None
        if cycle is not None:
            if latest_hold is not None and latest_hold.cycle_id == cycle.cycle_id:
                latest_route = "evidence"
            elif latest_human is not None and latest_human.cycle_id == cycle.cycle_id:
                latest_route = latest_human.route
        state = self.session.get(models.DishState, (generation_id, task_id))
        current_version_id = state.current_content_version_id if state is not None else None
        baseline = None
        if operation.phase == "held_evidence" and latest_hold is not None:
            baseline = latest_hold.baseline_content_version_id
        elif operation.phase == "held_human" and latest_human is not None:
            baseline = latest_human.baseline_content_version_id
        preconstruction_hold = bool(
            (active_hold is not None and active_hold.cycle_id is None)
            or (active_human is not None and active_human.cycle_id is None)
        )
        return WorkflowSnapshot(
            operation_status=operation.lifecycle,
            operation_phase=operation.phase,
            persisted_actions=tuple(operation.persisted_actions),
            live_status=_status_from_document(title, body),
            live_section_gid=live_section_gid,
            verification_queue_gid=verification_gid,
            verifier_established=verifier_established,
            latest_cycle_outcome=cycle.outcome if cycle else None,
            latest_cycle_route=latest_route,
            validation_rules=(),
            operation_kind=operation.kind,
            pending_steps=(),
            unresolved_attempts=(),
            migration_reconciliation_required=False,
            identity_matches=True,
            placement_matches=True,
            required_cycle_exists=cycle is not None,
            signoff_bound=signoff_bound,
            held_baseline_matches=(baseline is None or baseline == current_version_id),
            preconstruction_hold=preconstruction_hold,
            destination_repair_required=operation.phase == "ready_move_failed",
            dish_inspect_current=inspected,
        )

    def task_view(self, task_reference: str | uuid.UUID) -> TaskCurrentView:
        generation = self.active_generation()
        native = self._native_read_authority(generation.generation_id)
        task = self.resolve_task(task_reference)
        state = self.session.get(models.DishState, (generation.generation_id, task.task_id))
        membership = self.session.get(
            models.TaskMembershipHead, (generation.generation_id, task.task_id)
        )
        if state is None or (native is None and membership is None):
            raise ReadModelError("task has incomplete scalar/membership authority")
        if native is not None:
            if state.catalog_version_id != native.pointer.catalog_version_id:
                raise ReadModelError("task placement is stale against the native catalog")
            catalog_entry = self.session.scalar(
                select(models.SectionCatalogEntry).where(
                    models.SectionCatalogEntry.catalog_version_id
                    == native.pointer.catalog_version_id,
                    models.SectionCatalogEntry.section_id == state.section_id,
                )
            )
            if catalog_entry is None:
                raise ReadModelError("task Section is not in the active native catalog")
        version = self.session.get(models.ContentVersion, state.current_content_version_id)
        if version is None:
            raise ReadModelError("task authority bundle is incomplete")
        if state.archived_at is not None:
            completion_state = "archived"
        elif not state.completed:
            completion_state = "active"
        elif state.completion_reason == "cooked":
            completion_state = "cooked"
        else:
            completion_state = "completed"
        operation = self.session.scalar(
            select(wf.WorkflowOperation)
            .where(
                wf.WorkflowOperation.generation_id == generation.generation_id,
                wf.WorkflowOperation.task_id == task.task_id,
                wf.WorkflowOperation.lifecycle == "open",
            )
            .limit(1)
        )
        actions: tuple[str, ...] = ()
        if operation is not None:
            actions = tuple(
                legal_actions(
                    self._workflow_snapshot(
                        generation_id=generation.generation_id,
                        task_id=task.task_id,
                        title=version.title,
                        body=version.body,
                        operation=operation,
                    )
                )
            )
        return TaskCurrentView(
            task_id=task.task_id,
            title=version.title,
            body=version.body,
            content_version_id=version.content_version_id,
            dish_version=state.dish_version,
            task_revision=version.created_dish_version,
            membership_revision=(
                membership.membership_revision if membership is not None else None
            ),
            placement_revision=state.placement_version,
            completion_revision=state.completion_version,
            section_id=state.section_id,
            completed=state.completed,
            completion_reason=state.completion_reason,
            archived_at=state.archived_at,
            completion_state=completion_state,
            operation_id=operation.operation_id if operation else None,
            operation_phase=operation.phase if operation else None,
            operation_revision=operation.operation_revision if operation else None,
            legal_actions=actions,
            projection_freshness={"state": "not_configured", "stage": 4},
            read_authority=(native.identity if native is not None else {}),
        )
