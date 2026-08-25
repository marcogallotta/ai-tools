"""Set-oriented PostgreSQL read facts for the Stage 3 frontend board.

The query layer reads durable facts only. It does not infer legal actions and it
has no Asana/network dependency. Callers must execute bootstrap reads inside one
short coherent read transaction; HTTP activation remains gated on the final
PostgreSQL transaction/isolation and query-plan evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import Select, and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from dish_pg import models
from dish_pg import stage3_models as workflow
from dish_pg import stage5_models as projection


class BoardReadUnavailable(RuntimeError):
    """The active PostgreSQL board context cannot be established safely."""


@dataclass(frozen=True, slots=True)
class BoardContext:
    generation_id: UUID
    registry_version_id: UUID
    registry_revision: int
    evaluation_time: datetime


@dataclass(frozen=True, slots=True)
class SectionFact:
    section_id: UUID
    ordinal: int
    section_label: str
    workflow_role: str
    project_id: UUID
    project_label: str
    section_lifecycle: str
    project_lifecycle: str


@dataclass(frozen=True, slots=True)
class BoardRegistryFacts:
    context: BoardContext
    sections: tuple[SectionFact, ...]


@dataclass(frozen=True, slots=True)
class CardFact:
    section_id: UUID
    section_ordinal: int
    task_id: UUID
    title: str
    sort_title: str
    existence_state: str
    operation_kind: str | None
    operation_phase: str | None
    isolated: bool
    lease_attention: bool
    verification_attention: bool
    hold_active: bool
    recovery_required: bool
    abandonment_active: bool
    succession_active: bool
    projection_abnormal: bool


@dataclass(frozen=True, slots=True)
class BoardFacts:
    context: BoardContext
    sections: tuple[SectionFact, ...]
    cards_by_section: dict[UUID, tuple[CardFact, ...]]
    has_more_by_section: dict[UUID, bool]


@dataclass(frozen=True, slots=True)
class SectionPageFacts:
    context: BoardContext
    cards: tuple[CardFact, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class SearchFact:
    task_id: UUID
    title: str
    section_id: UUID
    section_label: str
    workflow_role: str
    project_label: str


@dataclass(frozen=True, slots=True)
class SearchFacts:
    results: tuple[SearchFact, ...]
    truncated: bool


class FrontendBoardQuery:
    """Bounded factual board reads over the currently active PG generation."""

    def __init__(self, session: Session):
        self.session = session

    def bootstrap_registry(self) -> BoardRegistryFacts:
        context = self.context()
        return BoardRegistryFacts(context=context, sections=self._sections(context))

    def bootstrap_cards(
        self,
        *,
        registry: BoardRegistryFacts,
        page_size: int,
        projection_delay: timedelta,
    ) -> BoardFacts:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if projection_delay <= timedelta(0):
            raise ValueError("projection_delay must be positive")
        context = registry.context
        sections = registry.sections
        rows = self.session.execute(
            self._bootstrap_cards_statement(
                context=context,
                page_size=page_size,
                projection_delay=projection_delay,
            )
        ).mappings()
        grouped: dict[UUID, list[CardFact]] = {section.section_id: [] for section in sections}
        has_more: dict[UUID, bool] = {section.section_id: False for section in sections}
        for row in rows:
            section_id = row["section_id"]
            target = grouped.get(section_id)
            if target is None:
                raise BoardReadUnavailable("board query returned a task outside the active registry")
            if len(target) < page_size:
                target.append(self._card_fact(row))
            else:
                has_more[section_id] = True
        return BoardFacts(
            context=context,
            sections=sections,
            cards_by_section={key: tuple(value) for key, value in grouped.items()},
            has_more_by_section=has_more,
        )

    def active_cards(
        self,
        *,
        registry: BoardRegistryFacts,
        projection_delay: timedelta,
        max_cards: int,
    ) -> tuple[CardFact, ...]:
        """Return all active board cards up to an explicit operator-read bound."""
        if max_cards <= 0:
            raise ValueError("max_cards must be positive")
        context = registry.context
        rows = list(
            self.session.execute(
                self._base_card_statement(context=context, projection_delay=projection_delay)
                .order_by(
                    models.SectionRegistryEntry.ordinal,
                    func.lower(models.ContentVersion.title),
                    models.DishTask.task_id,
                )
                .limit(max_cards + 1)
            ).mappings()
        )
        if len(rows) > max_cards:
            raise BoardReadUnavailable("frontend active-card capacity exceeded")
        return tuple(self._card_fact(row) for row in rows)

    def search_titles(
        self,
        *,
        query: str,
        projection_delay: timedelta,
        max_results: int,
        context: BoardContext | None = None,
    ) -> SearchFacts:
        """Return bounded active-board title matches from the full corpus."""
        if not query:
            raise ValueError("search query must be non-empty")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        context = context or self.context()
        normalized = query.lower()
        rows = list(
            self.session.execute(
                self._base_card_statement(
                    context=context,
                    projection_delay=projection_delay,
                )
                .where(
                    func.lower(models.ContentVersion.title).contains(
                        normalized,
                        autoescape=True,
                    )
                )
                .order_by(
                    func.lower(models.ContentVersion.title),
                    models.DishTask.task_id,
                )
                .limit(max_results + 1)
            ).mappings()
        )
        return SearchFacts(
            results=tuple(
                SearchFact(
                    task_id=row["task_id"],
                    title=row["title"],
                    section_id=row["section_id"],
                    section_label=row["section_label"],
                    workflow_role=row["workflow_role"],
                    project_label=row["project_label"],
                )
                for row in rows[:max_results]
            ),
            truncated=len(rows) > max_results,
        )

    def continuation(
        self,
        *,
        section_id: UUID,
        after_sort_title: str,
        after_task_id: UUID,
        page_size: int,
        projection_delay: timedelta,
    ) -> SectionPageFacts:
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if projection_delay <= timedelta(0):
            raise ValueError("projection_delay must be positive")
        context = self.context()
        rows = list(
            self.session.execute(
                self._continuation_statement(
                    context=context,
                    section_id=section_id,
                    after_sort_title=after_sort_title,
                    after_task_id=after_task_id,
                    page_size=page_size,
                    projection_delay=projection_delay,
                )
            ).mappings()
        )
        return SectionPageFacts(
            context=context,
            cards=tuple(self._card_fact(row) for row in rows[:page_size]),
            has_more=len(rows) > page_size,
        )

    def context(self) -> BoardContext:
        statement = (
            select(
                models.AuthorityGeneration.generation_id,
                models.ActiveSectionRegistry.registry_version_id,
                models.ActiveSectionRegistry.registry_revision,
                func.current_timestamp().label("evaluation_time"),
            )
            .join(
                models.ActiveSectionRegistry,
                models.ActiveSectionRegistry.generation_id
                == models.AuthorityGeneration.generation_id,
            )
            .where(models.AuthorityGeneration.status == "active")
            .limit(2)
        )
        rows = list(self.session.execute(statement).mappings())
        if len(rows) != 1:
            raise BoardReadUnavailable("exactly one active generation and registry are required")
        row = rows[0]
        evaluation_time = row["evaluation_time"]
        if evaluation_time.tzinfo is None:
            # SQLite test rendering loses tzinfo; PostgreSQL returns timestamptz.
            evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)
        return BoardContext(
            generation_id=row["generation_id"],
            registry_version_id=row["registry_version_id"],
            registry_revision=int(row["registry_revision"]),
            evaluation_time=evaluation_time,
        )

    def _sections(self, context: BoardContext) -> tuple[SectionFact, ...]:
        statement = (
            select(
                models.SectionRegistryEntry.section_id,
                models.SectionRegistryEntry.ordinal,
                case(
                    (
                        and_(
                            models.SectionRegistryEntry.display_name.like("Imported section %"),
                            models.SectionRegistryEntry.workflow_role == "research_queue",
                        ),
                        literal("Research Queue"),
                    ),
                    (
                        and_(
                            models.SectionRegistryEntry.display_name.like("Imported section %"),
                            models.SectionRegistryEntry.workflow_role == "verification_queue",
                        ),
                        literal("Verification Queue"),
                    ),
                    else_=models.SectionRegistryEntry.display_name,
                ).label("section_label"),
                models.SectionRegistryEntry.workflow_role,
                models.GovernedSection.project_id,
                models.GovernedProject.logical_name.label("project_label"),
                models.GovernedSection.lifecycle.label("section_lifecycle"),
                models.GovernedProject.lifecycle.label("project_lifecycle"),
            )
            .join(
                models.GovernedSection,
                models.GovernedSection.section_id == models.SectionRegistryEntry.section_id,
            )
            .join(
                models.GovernedProject,
                models.GovernedProject.project_id == models.GovernedSection.project_id,
            )
            .where(models.SectionRegistryEntry.registry_version_id == context.registry_version_id)
            .order_by(models.SectionRegistryEntry.ordinal)
        )
        return tuple(SectionFact(**dict(row)) for row in self.session.execute(statement).mappings())

    def attention_columns(
        self,
        *,
        generation_id: UUID,
        task_id,
        evaluation_time: datetime,
        projection_delay: timedelta,
    ) -> dict[str, object]:
        unresolved_execution = exists(
            select(literal(1))
            .select_from(workflow.CommandExecution)
            .where(
                workflow.CommandExecution.generation_id == generation_id,
                workflow.CommandExecution.task_id == task_id,
                workflow.CommandExecution.status == "uncertain",
                ~exists(
                    select(literal(1))
                    .select_from(workflow.RequestUncertaintyResolution)
                    .where(
                        workflow.RequestUncertaintyResolution.request_id
                        == workflow.CommandExecution.request_id
                    )
                ),
            )
        )
        active_succession = exists(
            select(literal(1))
            .select_from(workflow.OperationSuccessionEdge)
            .join(
                workflow.AbandonmentAttempt,
                workflow.AbandonmentAttempt.abandonment_id
                == workflow.OperationSuccessionEdge.abandonment_id,
            )
            .join(
                workflow.WorkflowOperation,
                workflow.WorkflowOperation.operation_id
                == workflow.OperationSuccessionEdge.successor_operation_id,
            )
            .where(
                workflow.OperationSuccessionEdge.task_id == task_id,
                workflow.AbandonmentAttempt.generation_id == generation_id,
                workflow.AbandonmentAttempt.task_id == task_id,
                workflow.AbandonmentAttempt.state == "published",
                workflow.AbandonmentAttempt.successor_operation_id
                == workflow.OperationSuccessionEdge.successor_operation_id,
                workflow.WorkflowOperation.generation_id == generation_id,
                workflow.WorkflowOperation.task_id == task_id,
                workflow.WorkflowOperation.lifecycle == "open",
            )
        )
        current_actor_lease = aliased(workflow.ServiceLease, name="current_actor_lease")
        later_actor_lease = aliased(workflow.ServiceLease, name="later_actor_lease")
        current_open_operation = aliased(
            workflow.WorkflowOperation, name="current_open_operation"
        )
        lease_attention = exists(
            select(literal(1))
            .select_from(current_actor_lease)
            .join(
                current_open_operation,
                current_open_operation.operation_id == current_actor_lease.operation_id,
            )
            .where(
                current_actor_lease.generation_id == generation_id,
                current_actor_lease.task_id == task_id,
                current_actor_lease.lease_kind == "actor",
                current_open_operation.generation_id == generation_id,
                current_open_operation.task_id == task_id,
                current_open_operation.lifecycle == "open",
                ~exists(
                    select(literal(1))
                    .select_from(later_actor_lease)
                    .where(
                        later_actor_lease.generation_id
                        == current_actor_lease.generation_id,
                        later_actor_lease.task_id == current_actor_lease.task_id,
                        later_actor_lease.operation_id
                        == current_actor_lease.operation_id,
                        later_actor_lease.lease_kind == "actor",
                        later_actor_lease.actor_attempt_sequence
                        > current_actor_lease.actor_attempt_sequence,
                    )
                ),
                or_(
                    current_actor_lease.state == "expired",
                    and_(
                        current_actor_lease.state == "active",
                        current_actor_lease.expires_at <= evaluation_time,
                    ),
                ),
            )
        )
        projection_cutoff = evaluation_time - projection_delay
        return {
            "lease_attention": lease_attention,
            "verification_attention": exists(
                select(literal(1))
                .select_from(workflow.HumanReviewRequirement)
                .where(
                    workflow.HumanReviewRequirement.generation_id == generation_id,
                    workflow.HumanReviewRequirement.task_id == task_id,
                    workflow.HumanReviewRequirement.route == "human_review",
                    workflow.HumanReviewRequirement.state == "open",
                )
            ),
            "hold_active": or_(
                exists(
                    select(literal(1))
                    .select_from(workflow.EvidenceHold)
                    .where(
                        workflow.EvidenceHold.generation_id == generation_id,
                        workflow.EvidenceHold.task_id == task_id,
                        workflow.EvidenceHold.state == "open",
                    )
                ),
                exists(
                    select(literal(1))
                    .select_from(workflow.HumanReviewRequirement)
                    .where(
                        workflow.HumanReviewRequirement.generation_id == generation_id,
                        workflow.HumanReviewRequirement.task_id == task_id,
                        workflow.HumanReviewRequirement.route == "two_pass_hold",
                        workflow.HumanReviewRequirement.state == "open",
                    )
                ),
            ),
            "recovery_required": unresolved_execution,
            "abandonment_active": exists(
                select(literal(1))
                .select_from(workflow.AbandonmentAttempt)
                .where(
                    workflow.AbandonmentAttempt.generation_id == generation_id,
                    workflow.AbandonmentAttempt.task_id == task_id,
                    workflow.AbandonmentAttempt.state.in_(
                        ("preparing", "published", "blocked", "reconciling")
                    ),
                )
            ),
            "succession_active": active_succession,
            "projection_abnormal": and_(
                exists(
                    select(literal(1))
                    .select_from(projection.ProjectionEpoch)
                    .where(
                        projection.ProjectionEpoch.generation_id == generation_id,
                        projection.ProjectionEpoch.status == "active",
                        projection.ProjectionEpoch.external_effects_enabled.is_(True),
                    )
                ),
                or_(
                    exists(
                        select(literal(1))
                        .select_from(projection.ProjectionDriftEvent)
                        .where(
                            projection.ProjectionDriftEvent.generation_id == generation_id,
                            projection.ProjectionDriftEvent.task_id == task_id,
                            projection.ProjectionDriftEvent.state == "open",
                        )
                    ),
                    exists(
                        select(literal(1))
                        .select_from(projection.ProjectionOutboxEvent)
                        .where(
                            projection.ProjectionOutboxEvent.generation_id == generation_id,
                            projection.ProjectionOutboxEvent.task_id == task_id,
                            projection.ProjectionOutboxEvent.origin == "live",
                            or_(
                                projection.ProjectionOutboxEvent.state.in_(("blocked", "uncertain")),
                                and_(
                                    projection.ProjectionOutboxEvent.state.in_(("pending", "claimed")),
                                    projection.ProjectionOutboxEvent.created_at <= projection_cutoff,
                                ),
                            ),
                        )
                    ),
                ),
            ),
        }

    def _base_card_statement(
        self, *, context: BoardContext, projection_delay: timedelta
    ) -> Select:
        task_id = models.DishTask.task_id
        sort_title = func.lower(models.ContentVersion.title)
        attention = self.attention_columns(
            generation_id=context.generation_id,
            task_id=task_id,
            evaluation_time=context.evaluation_time,
            projection_delay=projection_delay,
        )
        return (
            select(
                models.CurrentTaskSectionPlacement.section_id.label("section_id"),
                models.SectionRegistryEntry.ordinal.label("section_ordinal"),
                task_id.label("task_id"),
                models.ContentVersion.title.label("title"),
                case(
                    (
                        and_(
                            models.SectionRegistryEntry.display_name.like("Imported section %"),
                            models.SectionRegistryEntry.workflow_role == "research_queue",
                        ),
                        literal("Research Queue"),
                    ),
                    (
                        and_(
                            models.SectionRegistryEntry.display_name.like("Imported section %"),
                            models.SectionRegistryEntry.workflow_role == "verification_queue",
                        ),
                        literal("Verification Queue"),
                    ),
                    else_=models.SectionRegistryEntry.display_name,
                ).label("section_label"),
                models.SectionRegistryEntry.workflow_role.label("workflow_role"),
                models.GovernedProject.logical_name.label("project_label"),
                sort_title.label("sort_title"),
                models.DishTask.existence_state.label("existence_state"),
                workflow.WorkflowOperation.kind.label("operation_kind"),
                workflow.WorkflowOperation.phase.label("operation_phase"),
                (models.DishTask.existence_state == "isolated").label("isolated"),
                *(expr.label(name) for name, expr in attention.items()),
            )
            .select_from(models.DishTask)
            .join(
                models.TaskAuthorityHead,
                and_(
                    models.TaskAuthorityHead.generation_id == context.generation_id,
                    models.TaskAuthorityHead.task_id == task_id,
                ),
            )
            .join(
                models.ContentActivation,
                and_(
                    models.ContentActivation.content_activation_id
                    == models.TaskAuthorityHead.current_content_activation_id,
                    models.ContentActivation.generation_id == context.generation_id,
                    models.ContentActivation.task_id == task_id,
                ),
            )
            .join(
                models.ContentVersion,
                and_(
                    models.ContentVersion.generation_id == context.generation_id,
                    models.ContentVersion.task_id == task_id,
                    models.ContentVersion.content_version_id
                    == models.ContentActivation.content_version_id,
                ),
            )
            .join(
                models.CurrentTaskSectionPlacement,
                and_(
                    models.CurrentTaskSectionPlacement.generation_id == context.generation_id,
                    models.CurrentTaskSectionPlacement.task_id == task_id,
                ),
            )
            .join(
                models.SectionRegistryEntry,
                and_(
                    models.SectionRegistryEntry.registry_version_id
                    == context.registry_version_id,
                    models.SectionRegistryEntry.section_id
                    == models.CurrentTaskSectionPlacement.section_id,
                ),
            )
            .join(
                models.GovernedSection,
                models.GovernedSection.section_id
                == models.CurrentTaskSectionPlacement.section_id,
            )
            .join(
                models.GovernedProject,
                models.GovernedProject.project_id == models.GovernedSection.project_id,
            )
            .join(
                models.CurrentTaskProjectMembership,
                and_(
                    models.CurrentTaskProjectMembership.generation_id == context.generation_id,
                    models.CurrentTaskProjectMembership.task_id == task_id,
                    models.CurrentTaskProjectMembership.project_id
                    == models.GovernedSection.project_id,
                    models.CurrentTaskProjectMembership.is_member.is_(True),
                ),
            )
            .join(
                models.CurrentTaskCompletion,
                and_(
                    models.CurrentTaskCompletion.generation_id == context.generation_id,
                    models.CurrentTaskCompletion.task_id == task_id,
                    models.CurrentTaskCompletion.completed.is_(False),
                ),
            )
            .outerjoin(
                workflow.WorkflowOperation,
                and_(
                    workflow.WorkflowOperation.generation_id == context.generation_id,
                    workflow.WorkflowOperation.task_id == task_id,
                    workflow.WorkflowOperation.lifecycle == "open",
                ),
            )
            .where(
                models.DishTask.existence_state.in_(("ordinary", "isolated")),
                models.GovernedSection.lifecycle == "active",
                models.GovernedProject.lifecycle == "active",
            )
        )

    def _bootstrap_cards_statement(
        self, *, context: BoardContext, page_size: int, projection_delay: timedelta
    ) -> Select:
        base = self._base_card_statement(
            context=context, projection_delay=projection_delay
        ).subquery("board_cards")
        ranked = select(
            *base.c,
            func.row_number()
            .over(
                partition_by=base.c.section_id,
                order_by=(base.c.sort_title, base.c.task_id),
            )
            .label("row_number"),
        ).subquery("ranked_board_cards")
        return (
            select(*[column for column in ranked.c if column.key != "row_number"])
            .where(ranked.c.row_number <= page_size + 1)
            .order_by(ranked.c.section_ordinal, ranked.c.row_number)
        )

    def _continuation_statement(
        self,
        *,
        context: BoardContext,
        section_id: UUID,
        after_sort_title: str,
        after_task_id: UUID,
        page_size: int,
        projection_delay: timedelta,
    ) -> Select:
        base = self._base_card_statement(context=context, projection_delay=projection_delay)
        sort_title = func.lower(models.ContentVersion.title)
        return (
            base.where(
                models.CurrentTaskSectionPlacement.section_id == section_id,
                or_(
                    sort_title > after_sort_title,
                    and_(sort_title == after_sort_title, models.DishTask.task_id > after_task_id),
                ),
            )
            .order_by(sort_title, models.DishTask.task_id)
            .limit(page_size + 1)
        )

    @staticmethod
    def _card_fact(row) -> CardFact:
        return CardFact(
            section_id=row["section_id"],
            section_ordinal=int(row["section_ordinal"]),
            task_id=row["task_id"],
            title=row["title"],
            sort_title=row["sort_title"],
            existence_state=row["existence_state"],
            operation_kind=row["operation_kind"],
            operation_phase=row["operation_phase"],
            isolated=bool(row["isolated"]),
            lease_attention=bool(row["lease_attention"]),
            verification_attention=bool(row["verification_attention"]),
            hold_active=bool(row["hold_active"]),
            recovery_required=bool(row["recovery_required"]),
            abandonment_active=bool(row["abandonment_active"]),
            succession_active=bool(row["succession_active"]),
            projection_abnormal=bool(row["projection_abnormal"]),
        )
