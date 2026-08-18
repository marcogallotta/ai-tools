"""Stage 4 PostgreSQL authoritative reads and current-view construction."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions

from . import models
from . import stage3_models as wf
from .document_authority import CanonicalDocumentError, parse_canonical_document


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
    registry_version_id: uuid.UUID
    registry_revision: int


@dataclass(frozen=True)
class TaskCurrentView:
    task_id: uuid.UUID
    title: str
    body: str
    content_version_id: uuid.UUID
    task_revision: int
    membership_revision: int
    placement_revision: int
    completion_revision: int
    section_id: uuid.UUID
    completed: bool
    completion_reason: str
    completion_state: str
    operation_id: uuid.UUID | None
    operation_phase: str | None
    operation_revision: int | None
    legal_actions: tuple[str, ...]
    projection_freshness: Mapping[str, Any]


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

    def sections(self) -> tuple[dict[str, Any], ...]:
        generation = self.active_generation()
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

    def resolve_section(self, reference: str | uuid.UUID) -> models.GovernedSection:
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
        active = self._active_registry(generation.generation_id)
        section = self.resolve_section(section_reference)
        registered = self.session.scalar(
            select(models.SectionRegistryEntry).where(
                models.SectionRegistryEntry.registry_version_id == active.registry_version_id,
                models.SectionRegistryEntry.section_id == section.section_id,
            )
        )
        if registered is None:
            raise ReadModelError("section is not in the active registry")

        after_title: str | None = None
        after_task: uuid.UUID | None = None
        if cursor:
            payload = self.cursor_codec.decode(cursor)
            expected = {
                "generation_id": str(generation.generation_id),
                "registry_version_id": str(active.registry_version_id),
                "registry_revision": active.registry_revision,
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
                models.CurrentTaskCompletion.completed,
                models.TaskExternalAlias.external_id,
            )
            .join(
                models.CurrentTaskSectionPlacement,
                models.CurrentTaskSectionPlacement.task_id == models.DishTask.task_id,
            )
            .join(
                models.TaskAuthorityHead,
                and_(
                    models.TaskAuthorityHead.generation_id
                    == models.CurrentTaskSectionPlacement.generation_id,
                    models.TaskAuthorityHead.task_id == models.DishTask.task_id,
                ),
            )
            .join(
                models.ContentActivation,
                models.ContentActivation.content_activation_id
                == models.TaskAuthorityHead.current_content_activation_id,
            )
            .join(
                models.ContentVersion,
                models.ContentVersion.content_version_id
                == models.ContentActivation.content_version_id,
            )
            .join(
                models.CurrentTaskCompletion,
                and_(
                    models.CurrentTaskCompletion.generation_id
                    == models.TaskAuthorityHead.generation_id,
                    models.CurrentTaskCompletion.task_id == models.DishTask.task_id,
                ),
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
                models.CurrentTaskSectionPlacement.generation_id == generation.generation_id,
                models.CurrentTaskSectionPlacement.section_id == section.section_id,
                models.CurrentTaskSectionPlacement.registry_version_id == active.registry_version_id,
                models.CurrentTaskCompletion.completed.is_(False),
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
                    "registry_version_id": str(active.registry_version_id),
                    "registry_revision": active.registry_revision,
                    "section_id": str(section.section_id),
                    "page_size": page_size,
                    "after_title": last[1].lower(),
                    "after_task_id": str(last[0]),
                }
            )
        return TaskListPage(
            items=items,
            next_cursor=next_cursor,
            registry_version_id=active.registry_version_id,
            registry_revision=active.registry_revision,
        )

    def _workflow_snapshot(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        title: str,
        body: str,
        operation: wf.WorkflowOperation,
    ) -> WorkflowSnapshot:
        placement = self.session.get(models.CurrentTaskSectionPlacement, (generation_id, task_id))
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
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        activation = (
            self.session.get(models.ContentActivation, head.current_content_activation_id)
            if head is not None
            else None
        )
        current_version_id = activation.content_version_id if activation is not None else None
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
        task = self.resolve_task(task_reference)
        head = self.session.get(
            models.TaskAuthorityHead, (generation.generation_id, task.task_id)
        )
        if head is None:
            raise ReadModelError("task has no authority head in the active generation")
        activation = self.session.get(models.ContentActivation, head.current_content_activation_id)
        if activation is None:
            raise ReadModelError("task authority head has no content activation")
        version = self.session.get(models.ContentVersion, activation.content_version_id)
        placement = self.session.get(
            models.CurrentTaskSectionPlacement, (generation.generation_id, task.task_id)
        )
        completion = self.session.get(
            models.CurrentTaskCompletion, (generation.generation_id, task.task_id)
        )
        if version is None or placement is None or completion is None:
            raise ReadModelError("task authority bundle is incomplete")
        latest_completion = self.session.get(
            models.TaskCompletionEvent, completion.latest_event_id
        )
        if latest_completion is None:
            raise ReadModelError("task completion history is incomplete")
        if (
            latest_completion.task_id != task.task_id
            or latest_completion.generation_id != generation.generation_id
        ):
            raise ReadModelError("task completion history does not match current authority")
        if not completion.completed:
            completion_state = "active"
        elif latest_completion.reason == "cooked":
            completion_state = "cooked"
        elif latest_completion.reason == "archive":
            completion_state = "archived"
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
            task_revision=head.task_revision,
            membership_revision=head.membership_revision,
            placement_revision=head.placement_revision,
            completion_revision=head.completion_revision,
            section_id=placement.section_id,
            completed=completion.completed,
            completion_reason=latest_completion.reason,
            completion_state=completion_state,
            operation_id=operation.operation_id if operation else None,
            operation_phase=operation.phase if operation else None,
            operation_revision=operation.operation_revision if operation else None,
            legal_actions=actions,
            projection_freshness={"state": "not_configured", "stage": 4},
        )
