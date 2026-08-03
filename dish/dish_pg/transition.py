"""Stage 5 import, shadow, reconciliation, and projection services.

All services participate in a caller-owned SQLAlchemy transaction. No method
performs network I/O or commits. External workers persist an attempt before an
Asana call and persist exact reread evidence before adjudication.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from .planner import EffectObservation, adjudicate_effect
from .workflow import sha256_json


class TransitionAuthorityError(ValueError):
    pass


class ProjectionContentionLost(TransitionAuthorityError):
    pass


@dataclass(frozen=True)
class ProjectionClaim:
    event_id: uuid.UUID
    claim_token: uuid.UUID
    task_id: uuid.UUID
    aggregate_sequence: int
    event_type: str
    payload: Mapping[str, Any]
    idempotency_key: str


class SourceImportService:
    """Exact source-import provenance layered over Stage 2 import activation."""

    def __init__(self, session: Session, *, uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> None:
        self.session = session
        self.uuid_factory = uuid_factory

    def start_batch(
        self,
        *,
        import_batch_id: uuid.UUID,
        generation_id: uuid.UUID,
        import_run_id: uuid.UUID,
        source_release: str,
        source_commit: str,
        source_database_sha256: str,
        source_sidecars: Mapping[str, Any],
        ledger_through_commit: str,
        expected_entities: int,
        started_at: datetime,
    ) -> tx.SourceImportBatch:
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        run = self.session.get(models.ImportRun, import_run_id)
        if generation is None or generation.status not in {"pending", "active"}:
            raise TransitionAuthorityError("source import requires a pending or active generation")
        if run is None or run.status != "complete":
            raise TransitionAuthorityError("source import requires a complete Stage 2 capture run")
        row = tx.SourceImportBatch(
            import_batch_id=import_batch_id,
            generation_id=generation_id,
            import_run_id=import_run_id,
            source_release=source_release,
            source_commit=source_commit,
            source_database_sha256=source_database_sha256,
            source_sidecars=dict(source_sidecars),
            ledger_through_commit=ledger_through_commit,
            expected_entities=expected_entities,
            imported_entities=0,
            status="capturing",
            started_at=started_at,
            completed_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_entity(
        self,
        *,
        import_batch_id: uuid.UUID,
        entity_kind: str,
        source_identity: str,
        source_sha256: str,
        target_entity_type: str,
        target_entity_id: uuid.UUID,
        provenance: Mapping[str, Any],
        imported_at: datetime,
    ) -> tx.SourceImportEntityEvidence:
        batch = self.session.get(tx.SourceImportBatch, import_batch_id)
        if batch is None or batch.status != "capturing":
            raise TransitionAuthorityError("import batch is not capturing")
        existing = self.session.scalar(
            select(tx.SourceImportEntityEvidence).where(
                tx.SourceImportEntityEvidence.import_batch_id == import_batch_id,
                tx.SourceImportEntityEvidence.entity_kind == entity_kind,
                tx.SourceImportEntityEvidence.source_identity == source_identity,
            )
        )
        if existing is not None:
            if (
                existing.source_sha256 != source_sha256
                or existing.target_entity_type != target_entity_type
                or existing.target_entity_id != target_entity_id
                or existing.provenance != dict(provenance)
            ):
                raise TransitionAuthorityError("import source identity conflict")
            return existing
        if batch.imported_entities >= batch.expected_entities:
            raise TransitionAuthorityError("import batch would exceed expected entity count")
        row = tx.SourceImportEntityEvidence(
            evidence_id=self.uuid_factory(),
            import_batch_id=import_batch_id,
            entity_kind=entity_kind,
            source_identity=source_identity,
            source_sha256=source_sha256,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            provenance=dict(provenance),
            imported_at=imported_at,
        )
        self.session.add(row)
        batch.imported_entities += 1
        self.session.flush()
        return row

    def complete_batch(self, *, import_batch_id: uuid.UUID, completed_at: datetime) -> tx.SourceImportBatch:
        batch = self.session.get(tx.SourceImportBatch, import_batch_id)
        if batch is None or batch.status != "capturing":
            raise TransitionAuthorityError("import batch is not open")
        if batch.imported_entities != batch.expected_entities:
            raise TransitionAuthorityError("source import entity closure is incomplete")
        batch.status = "complete"
        batch.completed_at = completed_at
        self.session.flush()
        return batch


class ShadowService:
    """Gap-aware asynchronous shadow envelope and parity authority."""

    def __init__(self, session: Session, *, uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> None:
        self.session = session
        self.uuid_factory = uuid_factory

    def create_baseline(
        self,
        *,
        generation_id: uuid.UUID,
        source_generation_identity: str,
        source_commit: str,
        created_at: datetime,
    ) -> tx.ShadowBaseline:
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if generation is None or generation.status != "active":
            raise TransitionAuthorityError("shadow baseline requires active target generation")
        sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ShadowBaseline.baseline_sequence), 0)).where(
                    tx.ShadowBaseline.generation_id == generation_id
                )
            )
            or 0
        ) + 1
        row = tx.ShadowBaseline(
            shadow_baseline_id=self.uuid_factory(),
            generation_id=generation_id,
            source_generation_identity=source_generation_identity,
            source_commit=source_commit,
            baseline_sequence=sequence,
            status="open",
            disqualification_reason=None,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def capture_envelope(
        self,
        *,
        shadow_baseline_id: uuid.UUID,
        command_name: str,
        source_request_identity: str,
        canonical_input: Mapping[str, Any],
        source_outcome: Mapping[str, Any],
        source_post_state: Mapping[str, Any],
        captured_at: datetime,
    ) -> tx.ShadowEnvelope:
        baseline = self.session.get(tx.ShadowBaseline, shadow_baseline_id)
        if baseline is None or baseline.status != "open":
            raise TransitionAuthorityError("shadow baseline is not open")
        existing = self.session.scalar(
            select(tx.ShadowEnvelope).where(
                tx.ShadowEnvelope.shadow_baseline_id == shadow_baseline_id,
                tx.ShadowEnvelope.source_request_identity == source_request_identity,
            )
        )
        input_payload, outcome_payload = dict(canonical_input), dict(source_outcome)
        if existing is not None:
            if (
                existing.command_name != command_name
                or existing.canonical_input_sha256 != sha256_json(input_payload)
                or existing.source_outcome_sha256 != sha256_json(outcome_payload)
                or existing.source_post_state != dict(source_post_state)
            ):
                raise TransitionAuthorityError("shadow source request identity conflict")
            return existing
        envelope = tx.ShadowEnvelope(
            envelope_id=self.uuid_factory(),
            shadow_baseline_id=shadow_baseline_id,
            command_name=command_name,
            source_request_identity=source_request_identity,
            canonical_input=input_payload,
            canonical_input_sha256=sha256_json(input_payload),
            source_outcome=outcome_payload,
            source_outcome_sha256=sha256_json(outcome_payload),
            source_post_state=dict(source_post_state),
            captured_at=captured_at,
        )
        delivery = tx.ShadowDelivery(
            delivery_id=self.uuid_factory(),
            envelope_id=envelope.envelope_id,
            state="pending",
            claim_owner=None,
            claim_token=None,
            claim_expires_at=None,
            delivery_revision=1,
            attempts=0,
            last_error=None,
            created_at=captured_at,
            terminal_at=None,
        )
        self.session.add(envelope)
        self.session.flush()
        self.session.add(delivery)
        self.session.flush()
        return envelope

    def claim_delivery(
        self,
        *,
        worker_id: str,
        claim_token: uuid.UUID,
        now: datetime,
        ttl: timedelta,
    ) -> tx.ShadowDelivery | None:
        candidates = self.session.scalars(
            select(tx.ShadowDelivery)
            .where(
                (tx.ShadowDelivery.state == "pending")
                | (
                    (tx.ShadowDelivery.state == "claimed")
                    & (tx.ShadowDelivery.claim_expires_at <= now)
                )
            )
            .order_by(tx.ShadowDelivery.created_at, tx.ShadowDelivery.delivery_id)
        ).all()
        for row in candidates:
            revision = row.delivery_revision
            prior_state = row.state
            result = self.session.execute(
                update(tx.ShadowDelivery)
                .where(
                    tx.ShadowDelivery.delivery_id == row.delivery_id,
                    tx.ShadowDelivery.delivery_revision == revision,
                    tx.ShadowDelivery.state == prior_state,
                )
                .values(
                    state="claimed",
                    claim_owner=worker_id,
                    claim_token=claim_token,
                    claim_expires_at=now + ttl,
                    delivery_revision=revision + 1,
                    attempts=row.attempts + 1,
                    last_error=None,
                )
            )
            if result.rowcount == 1:
                self.session.flush()
                self.session.expire(row)
                return row
        return None

    def compare_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        claim_token: uuid.UUID,
        target_result: Mapping[str, Any],
        comparator_release: str,
        compared_at: datetime,
        semantic_normalizer: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> tx.ShadowComparison:
        delivery = self.session.get(tx.ShadowDelivery, delivery_id)
        if delivery is None or delivery.state != "claimed" or delivery.claim_token != claim_token:
            raise TransitionAuthorityError("shadow delivery claim does not match")
        envelope = self.session.get(tx.ShadowEnvelope, delivery.envelope_id)
        target = dict(target_result)
        if envelope.source_outcome == target:
            parity, differences = "exact", []
        elif semantic_normalizer and semantic_normalizer(envelope.source_outcome) == semantic_normalizer(target):
            parity, differences = "semantic", []
        else:
            parity = "mismatch"
            differences = [{"source": envelope.source_outcome, "target": target}]
        comparison = tx.ShadowComparison(
            comparison_id=self.uuid_factory(),
            envelope_id=envelope.envelope_id,
            target_result=target,
            target_result_sha256=sha256_json(target),
            parity_class=parity,
            differences=differences,
            comparator_release=comparator_release,
            compared_at=compared_at,
        )
        self.session.add(comparison)
        delivery.state = "delivered"
        delivery.claim_owner = None
        delivery.claim_token = None
        delivery.claim_expires_at = None
        delivery.delivery_revision += 1
        delivery.terminal_at = compared_at
        if parity == "mismatch":
            self._open_gap(
                baseline_id=envelope.shadow_baseline_id,
                envelope_id=envelope.envelope_id,
                identity=f"mismatch:{envelope.source_request_identity}",
                kind="mismatch",
                details={"differences": differences},
                at=compared_at,
            )
        self.session.flush()
        return comparison

    def fail_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        claim_token: uuid.UUID,
        error: str,
        failed_at: datetime,
    ) -> tx.ShadowGap:
        delivery = self.session.get(tx.ShadowDelivery, delivery_id)
        if delivery is None or delivery.state != "claimed" or delivery.claim_token != claim_token:
            raise TransitionAuthorityError("shadow delivery claim does not match")
        envelope = self.session.get(tx.ShadowEnvelope, delivery.envelope_id)
        delivery.state = "failed"
        delivery.claim_owner = None
        delivery.claim_token = None
        delivery.claim_expires_at = None
        delivery.delivery_revision += 1
        delivery.last_error = error
        delivery.terminal_at = failed_at
        gap = self._open_gap(
            baseline_id=envelope.shadow_baseline_id,
            envelope_id=envelope.envelope_id,
            identity=f"delivery:{envelope.source_request_identity}",
            kind="delivery_failure",
            details={"error": error},
            at=failed_at,
        )
        self.session.flush()
        return gap

    def _open_gap(
        self,
        *,
        baseline_id: uuid.UUID,
        envelope_id: uuid.UUID | None,
        identity: str,
        kind: str,
        details: Mapping[str, Any],
        at: datetime,
    ) -> tx.ShadowGap:
        existing = self.session.scalar(
            select(tx.ShadowGap).where(
                tx.ShadowGap.shadow_baseline_id == baseline_id,
                tx.ShadowGap.gap_identity == identity,
            )
        )
        if existing is not None:
            return existing
        row = tx.ShadowGap(
            gap_id=self.uuid_factory(),
            shadow_baseline_id=baseline_id,
            envelope_id=envelope_id,
            gap_identity=identity,
            gap_kind=kind,
            state="open",
            details=dict(details),
            resolution=None,
            gap_revision=1,
            created_at=at,
            resolved_at=None,
        )
        self.session.add(row)
        return row

    def record_gap(
        self,
        *,
        baseline_id: uuid.UUID,
        gap_identity: str,
        gap_kind: str,
        details: Mapping[str, Any],
        created_at: datetime,
        envelope_id: uuid.UUID | None = None,
    ) -> tx.ShadowGap:
        baseline = self.session.get(tx.ShadowBaseline, baseline_id)
        if baseline is None or baseline.status != "open":
            raise TransitionAuthorityError("shadow baseline is not open")
        if gap_kind not in {"missing_envelope", "delivery_failure", "uncomparable", "mismatch"}:
            raise TransitionAuthorityError("unknown shadow gap kind")
        row = self._open_gap(
            baseline_id=baseline_id,
            envelope_id=envelope_id,
            identity=gap_identity,
            kind=gap_kind,
            details=details,
            at=created_at,
        )
        self.session.flush()
        return row

    def resolve_gap(
        self,
        *,
        gap_id: uuid.UUID,
        resolution: Mapping[str, Any],
        resolved_at: datetime,
        waived: bool = False,
    ) -> tx.ShadowGap:
        gap = self.session.get(tx.ShadowGap, gap_id)
        if gap is None or gap.state != "open":
            raise TransitionAuthorityError("shadow gap is not open")
        gap.state = "waived" if waived else "resolved"
        gap.resolution = dict(resolution)
        gap.gap_revision += 1
        gap.resolved_at = resolved_at
        if gap.gap_kind == "delivery_failure" and gap.envelope_id is not None and not waived:
            delivery = self.session.scalar(
                select(tx.ShadowDelivery).where(
                    tx.ShadowDelivery.envelope_id == gap.envelope_id
                )
            )
            if delivery is not None and delivery.state == "failed":
                delivery.state = "pending"
                delivery.terminal_at = None
                delivery.delivery_revision += 1
        self.session.flush()
        return gap

    def close_baseline(self, *, baseline_id: uuid.UUID, closed_at: datetime) -> tx.ShadowBaseline:
        baseline = self.session.get(tx.ShadowBaseline, baseline_id)
        if baseline is None or baseline.status != "open":
            raise TransitionAuthorityError("shadow baseline is not open")
        open_gaps = self.session.scalar(
            select(func.count()).select_from(tx.ShadowGap).where(
                tx.ShadowGap.shadow_baseline_id == baseline_id,
                tx.ShadowGap.state == "open",
            )
        )
        incomplete = self.session.scalar(
            select(func.count())
            .select_from(tx.ShadowDelivery)
            .join(tx.ShadowEnvelope, tx.ShadowEnvelope.envelope_id == tx.ShadowDelivery.envelope_id)
            .where(
                tx.ShadowEnvelope.shadow_baseline_id == baseline_id,
                tx.ShadowDelivery.state != "delivered",
            )
        )
        if open_gaps or incomplete:
            raise TransitionAuthorityError("shadow baseline has unresolved gaps or deliveries")
        baseline.status = "closed"
        baseline.terminal_at = closed_at
        self.session.flush()
        return baseline

    def disqualify_stale_baselines(
        self, *, active_generation_id: uuid.UUID, reason: str, at: datetime
    ) -> int:
        rows = self.session.scalars(
            select(tx.ShadowBaseline).where(
                tx.ShadowBaseline.status == "open",
                tx.ShadowBaseline.generation_id != active_generation_id,
            )
        ).all()
        for row in rows:
            row.status = "disqualified"
            row.disqualification_reason = reason
            row.terminal_at = at
        self.session.flush()
        return len(rows)


class ProjectionService:
    """Transactional outbox, mapping, attempt, drift, and reconciliation authority."""

    def __init__(self, session: Session, *, uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> None:
        self.session = session
        self.uuid_factory = uuid_factory

    def activate_epoch(
        self,
        *,
        generation_id: uuid.UUID,
        activation_reason: str,
        created_at: datetime,
    ) -> tx.ProjectionEpoch:
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if generation is None or generation.status != "active":
            raise TransitionAuthorityError("projection epoch requires active authority generation")
        active = self.session.scalar(
            select(tx.ProjectionEpoch).where(
                tx.ProjectionEpoch.generation_id == generation_id,
                tx.ProjectionEpoch.status == "active",
            )
        )
        if active is not None:
            return active
        number = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ProjectionEpoch.epoch_number), 0)).where(
                    tx.ProjectionEpoch.generation_id == generation_id
                )
            )
            or 0
        ) + 1
        row = tx.ProjectionEpoch(
            projection_epoch_id=self.uuid_factory(),
            generation_id=generation_id,
            epoch_number=number,
            status="active",
            activation_reason=activation_reason,
            created_at=created_at,
            retired_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def retire_epoch(self, *, projection_epoch_id: uuid.UUID, retired_at: datetime) -> None:
        epoch = self.session.get(tx.ProjectionEpoch, projection_epoch_id)
        if epoch is None or epoch.status != "active":
            raise TransitionAuthorityError("projection epoch is not active")
        epoch.status = "retired"
        epoch.retired_at = retired_at
        for mapping_model in (
            tx.ProjectProjectionMapping,
            tx.SectionProjectionMapping,
            tx.TaskProjectionMapping,
        ):
            mappings = self.session.scalars(
                select(mapping_model).where(
                    mapping_model.projection_epoch_id == projection_epoch_id,
                    mapping_model.state == "active",
                )
            ).all()
            for mapping in mappings:
                mapping.state = "retired"
                mapping.mapping_revision += 1
                mapping.retired_at = retired_at
        events = self.session.scalars(
            select(tx.ProjectionOutboxEvent).where(
                tx.ProjectionOutboxEvent.projection_epoch_id == projection_epoch_id,
                tx.ProjectionOutboxEvent.state.not_in(("applied", "superseded")),
            )
        ).all()
        for event in events:
            event.state = "superseded"
            event.claim_owner = None
            event.claim_token = None
            event.claim_expires_at = None
            event.outbox_revision += 1
            event.terminal_at = retired_at
            attempts = self.session.scalars(
                select(tx.ProjectionAttempt).where(
                    tx.ProjectionAttempt.projection_event_id == event.projection_event_id,
                    tx.ProjectionAttempt.state.in_(("dispatched", "uncertain", "blocked")),
                )
            ).all()
            for attempt in attempts:
                attempt.state = "blocked"
                attempt.terminal_at = retired_at
        self.session.flush()

    def record(
        self,
        *,
        generation_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> uuid.UUID:
        return self._record_event(
            generation_id=generation_id,
            execution_id=execution_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            source_route="command",
            created_at=created_at,
        ).projection_event_id

    def _record_event(
        self,
        *,
        generation_id: uuid.UUID,
        execution_id: uuid.UUID | None,
        task_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any],
        source_route: str,
        created_at: datetime,
    ) -> tx.ProjectionOutboxEvent:
        epoch = self.session.scalar(
            select(tx.ProjectionEpoch).where(
                tx.ProjectionEpoch.generation_id == generation_id,
                tx.ProjectionEpoch.status == "active",
            )
        )
        if epoch is None:
            raise TransitionAuthorityError("no active projection epoch")
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if generation is None or generation.status != "active":
            raise TransitionAuthorityError("projection event requires active authority generation")
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        if head is None:
            raise TransitionAuthorityError("projection event requires a task in the active generation")
        if source_route == "command":
            execution = self.session.get(wf.CommandExecution, execution_id)
            if (
                execution is None
                or execution.generation_id != generation_id
                or execution.task_id != task_id
                or execution.status not in {"claimed", "committed"}
            ):
                raise TransitionAuthorityError(
                    "command projection event requires the exact task-bound execution"
                )
        identity_payload = {
            "generation_id": str(generation_id),
            "execution_id": str(execution_id) if execution_id else None,
            "task_id": str(task_id),
            "event_type": event_type,
            "payload": dict(payload),
            "source_route": source_route,
        }
        key = sha256_json(identity_payload)
        existing = self.session.scalar(
            select(tx.ProjectionOutboxEvent).where(tx.ProjectionOutboxEvent.idempotency_key == key)
        )
        if existing is not None:
            return existing
        sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ProjectionOutboxEvent.aggregate_sequence), 0)).where(
                    tx.ProjectionOutboxEvent.generation_id == generation_id,
                    tx.ProjectionOutboxEvent.task_id == task_id,
                )
            )
            or 0
        ) + 1
        event_id = self.uuid_factory()
        intent = dict(payload)
        marker = None
        if event_type == "create_task":
            marker = f"dish-create:{task_id}:{event_id}"
            intent["correlation_marker"] = marker
        row = tx.ProjectionOutboxEvent(
            projection_event_id=event_id,
            generation_id=generation_id,
            projection_epoch_id=epoch.projection_epoch_id,
            source_route=source_route,
            command_execution_id=execution_id,
            task_id=task_id,
            event_type=event_type,
            aggregate_sequence=sequence,
            idempotency_key=key,
            intent_payload=intent,
            intent_sha256=sha256_json(intent),
            state="pending",
            claim_owner=None,
            claim_token=None,
            claim_expires_at=None,
            outbox_revision=1,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        if marker is not None:
            self.session.add(
                tx.ProjectionCreateCorrelation(
                    correlation_id=self.uuid_factory(),
                    projection_event_id=event_id,
                    marker=marker,
                    state="pending",
                    matched_external_id=None,
                    match_count=None,
                    mapping_id=None,
                    correlation_revision=1,
                    last_evidence={},
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            self.session.flush()
        return row

    def bind_imported_mappings(
        self,
        *,
        generation_id: uuid.UUID,
        bound_at: datetime,
    ) -> tuple[int, int, int]:
        epoch = self._active_epoch(generation_id)
        active_registry = self.session.get(models.ActiveSectionRegistry, generation_id)
        if active_registry is None:
            raise TransitionAuthorityError("imported mappings require the active section registry")
        valid_section_ids = set(
            self.session.scalars(
                select(models.SectionRegistryEntry.section_id).where(
                    models.SectionRegistryEntry.registry_version_id
                    == active_registry.registry_version_id
                )
            )
        )
        valid_project_ids = set(
            self.session.scalars(
                select(models.GovernedSection.project_id).where(
                    models.GovernedSection.section_id.in_(valid_section_ids)
                )
            )
        )
        valid_task_ids = set(
            self.session.scalars(
                select(models.TaskAuthorityHead.task_id).where(
                    models.TaskAuthorityHead.generation_id == generation_id
                )
            )
        )

        def has_active_mapping(mapping_model, alias_id: uuid.UUID) -> bool:
            return self.session.scalar(
                select(mapping_model.mapping_id).where(
                    mapping_model.alias_id == alias_id,
                    mapping_model.state == "active",
                )
            ) is not None

        project_count = section_count = task_count = 0
        project_aliases = self.session.scalars(
            select(models.ProjectExternalAlias).where(
                models.ProjectExternalAlias.origin == "imported",
                models.ProjectExternalAlias.state == "active",
                models.ProjectExternalAlias.project_id.in_(valid_project_ids),
            )
        )
        for alias in project_aliases:
            if has_active_mapping(tx.ProjectProjectionMapping, alias.alias_id):
                continue
            self.session.add(
                tx.ProjectProjectionMapping(
                    mapping_id=self.uuid_factory(),
                    generation_id=generation_id,
                    projection_epoch_id=epoch.projection_epoch_id,
                    project_id=alias.project_id,
                    alias_id=alias.alias_id,
                    state="active",
                    mapping_revision=1,
                    bound_at=bound_at,
                    retired_at=None,
                )
            )
            project_count += 1
        section_aliases = self.session.scalars(
            select(models.SectionExternalAlias).where(
                models.SectionExternalAlias.origin == "imported",
                models.SectionExternalAlias.state == "active",
                models.SectionExternalAlias.section_id.in_(valid_section_ids),
            )
        )
        for alias in section_aliases:
            if has_active_mapping(tx.SectionProjectionMapping, alias.alias_id):
                continue
            self.session.add(
                tx.SectionProjectionMapping(
                    mapping_id=self.uuid_factory(),
                    generation_id=generation_id,
                    projection_epoch_id=epoch.projection_epoch_id,
                    section_id=alias.section_id,
                    alias_id=alias.alias_id,
                    state="active",
                    mapping_revision=1,
                    bound_at=bound_at,
                    retired_at=None,
                )
            )
            section_count += 1
        task_aliases = self.session.scalars(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.origin == "imported",
                models.TaskExternalAlias.state == "active",
                models.TaskExternalAlias.task_id.in_(valid_task_ids),
            )
        )
        for alias in task_aliases:
            if has_active_mapping(tx.TaskProjectionMapping, alias.alias_id):
                continue
            self.session.add(
                tx.TaskProjectionMapping(
                    mapping_id=self.uuid_factory(),
                    generation_id=generation_id,
                    projection_epoch_id=epoch.projection_epoch_id,
                    task_id=alias.task_id,
                    alias_id=alias.alias_id,
                    state="active",
                    mapping_revision=1,
                    bound_at=bound_at,
                    retired_at=None,
                )
            )
            task_count += 1
        self.session.flush()
        return project_count, section_count, task_count

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> ProjectionClaim | None:
        candidates = self.session.scalars(
            select(tx.ProjectionOutboxEvent)
            .join(
                tx.ProjectionEpoch,
                tx.ProjectionEpoch.projection_epoch_id
                == tx.ProjectionOutboxEvent.projection_epoch_id,
            )
            .join(
                models.AuthorityGeneration,
                models.AuthorityGeneration.generation_id
                == tx.ProjectionOutboxEvent.generation_id,
            )
            .where(
                tx.ProjectionEpoch.status == "active",
                models.AuthorityGeneration.status == "active",
                (tx.ProjectionOutboxEvent.state == "pending")
                | (
                    (tx.ProjectionOutboxEvent.state == "claimed")
                    & (tx.ProjectionOutboxEvent.claim_expires_at <= now)
                ),
            )
            .order_by(
                tx.ProjectionOutboxEvent.created_at,
                tx.ProjectionOutboxEvent.task_id,
                tx.ProjectionOutboxEvent.aggregate_sequence,
            )
        ).all()
        for event in candidates:
            blockers = self.session.scalar(
                select(func.count()).select_from(tx.ProjectionOutboxEvent).where(
                    tx.ProjectionOutboxEvent.generation_id == event.generation_id,
                    tx.ProjectionOutboxEvent.task_id == event.task_id,
                    tx.ProjectionOutboxEvent.aggregate_sequence < event.aggregate_sequence,
                    tx.ProjectionOutboxEvent.state.not_in(("applied", "superseded")),
                )
            )
            if blockers:
                continue
            token = self.uuid_factory()
            revision, prior_state = event.outbox_revision, event.state
            result = self.session.execute(
                update(tx.ProjectionOutboxEvent)
                .where(
                    tx.ProjectionOutboxEvent.projection_event_id == event.projection_event_id,
                    tx.ProjectionOutboxEvent.outbox_revision == revision,
                    tx.ProjectionOutboxEvent.state == prior_state,
                )
                .values(
                    state="claimed",
                    claim_owner=worker_id,
                    claim_token=token,
                    claim_expires_at=now + ttl,
                    outbox_revision=revision + 1,
                    terminal_at=None,
                )
            )
            if result.rowcount == 1:
                self.session.flush()
                return ProjectionClaim(
                    event.projection_event_id,
                    token,
                    event.task_id,
                    event.aggregate_sequence,
                    event.event_type,
                    dict(event.intent_payload),
                    event.idempotency_key,
                )
        return None

    def begin_attempt(
        self,
        *,
        event_id: uuid.UUID,
        claim_token: uuid.UUID,
        worker_id: str,
        request_identity: str,
        request_payload: Mapping[str, Any],
        intended_external_id: str | None,
        started_at: datetime,
    ) -> tx.ProjectionAttempt:
        event = self.session.get(tx.ProjectionOutboxEvent, event_id)
        if (
            event is None
            or event.state != "claimed"
            or event.claim_token != claim_token
            or event.claim_owner != worker_id
        ):
            raise TransitionAuthorityError("projection claim does not match")
        epoch = self.session.get(tx.ProjectionEpoch, event.projection_epoch_id)
        generation = self.session.get(models.AuthorityGeneration, event.generation_id)
        if (
            epoch is None
            or epoch.status != "active"
            or generation is None
            or generation.status != "active"
        ):
            raise TransitionAuthorityError("stale projection epoch cannot dispatch an attempt")
        existing = self.session.scalar(
            select(tx.ProjectionAttempt).where(tx.ProjectionAttempt.request_identity == request_identity)
        )
        payload = dict(request_payload)
        if existing is not None:
            if existing.projection_event_id != event_id or existing.request_sha256 != sha256_json(payload):
                raise TransitionAuthorityError("projection request identity conflict")
            return existing
        number = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ProjectionAttempt.attempt_number), 0)).where(
                    tx.ProjectionAttempt.projection_event_id == event_id
                )
            )
            or 0
        ) + 1
        row = tx.ProjectionAttempt(
            attempt_id=self.uuid_factory(),
            projection_event_id=event_id,
            attempt_number=number,
            worker_id=worker_id,
            request_identity=request_identity,
            intended_external_id=intended_external_id,
            request_payload=payload,
            request_sha256=sha256_json(payload),
            state="dispatched",
            started_at=started_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_observation_and_adjudicate(
        self,
        *,
        attempt_id: uuid.UUID,
        observation_kind: str,
        observed_applied: bool | None,
        observed_identity: str | None,
        reread_complete: bool,
        evidence: Mapping[str, Any],
        decided_by: str,
        decision_reason: str,
        observed_at: datetime,
    ) -> tx.ProjectionAdjudication:
        attempt = self.session.get(tx.ProjectionAttempt, attempt_id)
        if attempt is None or attempt.state not in {"dispatched", "uncertain", "blocked"}:
            raise TransitionAuthorityError("projection attempt is not unresolved")
        event = self.session.get(tx.ProjectionOutboxEvent, attempt.projection_event_id)
        if event is None or event.state not in {"claimed", "uncertain", "blocked"}:
            raise TransitionAuthorityError("projection event is not unresolved")
        observation_sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ProjectionObservation.observation_sequence), 0)).where(
                    tx.ProjectionObservation.attempt_id == attempt_id
                )
            )
            or 0
        ) + 1
        adjudication_sequence = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ProjectionAdjudication.adjudication_sequence), 0)).where(
                    tx.ProjectionAdjudication.attempt_id == attempt_id
                )
            )
            or 0
        ) + 1
        observation = tx.ProjectionObservation(
            observation_id=self.uuid_factory(),
            attempt_id=attempt_id,
            observation_sequence=observation_sequence,
            observation_kind=observation_kind,
            observed_applied=observed_applied,
            observed_identity=observed_identity,
            reread_complete=reread_complete,
            evidence=dict(evidence),
            evidence_sha256=sha256_json(dict(evidence)),
            observed_at=observed_at,
        )
        self.session.add(observation)
        self.session.flush()
        decision = adjudicate_effect(
            intended_identity=event.idempotency_key,
            observation=EffectObservation(
                intended_identity=event.idempotency_key,
                observed_identity=observed_identity,
                observed_applied=observed_applied,
                reread_complete=reread_complete,
                evidence=dict(evidence),
            ),
        )
        outcome = decision.outcome
        if event.event_type == "create_task":
            correlation = self.session.scalar(
                select(tx.ProjectionCreateCorrelation).where(
                    tx.ProjectionCreateCorrelation.projection_event_id
                    == event.projection_event_id
                )
            )
            if outcome == "confirmed" and (correlation is None or correlation.state != "bound"):
                outcome = "blocked"
                decision_reason = "create confirmation lacks one exact bound correlation marker"
            elif outcome == "not_applied" and (
                correlation is None or correlation.state != "not_found"
            ):
                outcome = "blocked"
                decision_reason = "create non-application lacks one exact zero-match marker search"
            elif correlation is not None and correlation.state == "ambiguous":
                outcome = "blocked"
                decision_reason = "create marker search remains ambiguous"
        adjudication = tx.ProjectionAdjudication(
            adjudication_id=self.uuid_factory(),
            attempt_id=attempt_id,
            observation_id=observation.observation_id,
            adjudication_sequence=adjudication_sequence,
            outcome=outcome,
            decided_by=decided_by,
            decision_reason=decision_reason,
            decided_at=observed_at,
        )
        self.session.add(adjudication)
        attempt.state = outcome
        attempt.terminal_at = observed_at
        event.claim_owner = None
        event.claim_token = None
        event.claim_expires_at = None
        event.outbox_revision += 1
        if outcome == "confirmed":
            event.state = "applied"
            event.terminal_at = observed_at
        elif outcome == "not_applied":
            event.state = "pending"
            event.terminal_at = None
        elif outcome == "uncertain":
            event.state = "uncertain"
            event.terminal_at = observed_at
        else:
            event.state = "blocked"
            event.terminal_at = observed_at
        self.session.flush()
        return adjudication

    def resolve_create_correlation(
        self,
        *,
        event_id: uuid.UUID,
        attempt_id: uuid.UUID,
        external_matches: Sequence[str],
        observed_at: datetime,
        evidence: Mapping[str, Any],
    ) -> tx.ProjectionCreateCorrelation:
        event = self.session.get(tx.ProjectionOutboxEvent, event_id)
        correlation = self.session.scalar(
            select(tx.ProjectionCreateCorrelation).where(
                tx.ProjectionCreateCorrelation.projection_event_id == event_id
            )
        )
        if event is None or event.event_type != "create_task" or correlation is None:
            raise TransitionAuthorityError("event has no create correlation authority")
        matches = tuple(dict.fromkeys(str(value) for value in external_matches))
        if correlation.state == "bound":
            if len(matches) == 1 and matches[0] == correlation.matched_external_id:
                return correlation
            raise TransitionAuthorityError("bound create correlation cannot be rebound")
        attempt = self.session.get(tx.ProjectionAttempt, attempt_id)
        if (
            attempt is None
            or attempt.projection_event_id != event_id
            or attempt.state not in {"dispatched", "uncertain", "blocked"}
        ):
            raise TransitionAuthorityError(
                "create correlation requires the exact unresolved projection attempt"
            )
        correlation.correlation_revision += 1
        correlation.match_count = len(matches)
        correlation.last_evidence = dict(evidence)
        correlation.updated_at = observed_at
        if len(matches) == 0:
            correlation.state = "not_found"
            correlation.matched_external_id = None
            correlation.mapping_id = None
        elif len(matches) > 1:
            correlation.state = "ambiguous"
            correlation.matched_external_id = None
            correlation.mapping_id = None
            event.state = "blocked"
            event.claim_owner = None
            event.claim_token = None
            event.claim_expires_at = None
            event.outbox_revision += 1
            event.terminal_at = observed_at
            attempt.state = "blocked"
            attempt.terminal_at = observed_at
        else:
            external_id = matches[0]
            if not external_id.isdigit() or external_id.startswith("0"):
                raise TransitionAuthorityError("Asana correlation match must be a canonical GID")
            epoch = self._active_epoch(event.generation_id)
            alias = models.TaskExternalAlias(
                alias_id=self.uuid_factory(),
                task_id=event.task_id,
                external_system="asana",
                external_id=external_id,
                origin="projection",
                import_run_id=None,
                projection_event_id=event_id,
                state="active",
                created_at=observed_at,
                retired_at=None,
            )
            self.session.add(alias)
            self.session.flush()
            mapping = tx.TaskProjectionMapping(
                mapping_id=self.uuid_factory(),
                generation_id=event.generation_id,
                projection_epoch_id=epoch.projection_epoch_id,
                task_id=event.task_id,
                alias_id=alias.alias_id,
                state="active",
                mapping_revision=1,
                bound_at=observed_at,
                retired_at=None,
            )
            self.session.add(mapping)
            self.session.flush()
            correlation.state = "bound"
            correlation.matched_external_id = external_id
            correlation.mapping_id = mapping.mapping_id
        self.session.flush()
        return correlation

    def recover(
        self,
        *,
        attempt_id: uuid.UUID,
        route: str,
        arguments: Mapping[str, Any],
        actor: str,
        recovered_at: datetime,
        expected_task_id: uuid.UUID | None = None,
    ) -> Mapping[str, Any]:
        if route not in {"recover", "repair-destination"}:
            raise TransitionAuthorityError("unknown projection recovery route")
        attempt = self.session.get(tx.ProjectionAttempt, attempt_id)
        if attempt is None or attempt.state not in {"dispatched", "uncertain", "blocked"}:
            raise TransitionAuthorityError("recover targets one exact unresolved projection attempt")
        event = self.session.get(tx.ProjectionOutboxEvent, attempt.projection_event_id)
        if event is None or event.state not in {"claimed", "uncertain", "blocked"}:
            raise TransitionAuthorityError("recover target event is not unresolved")
        if expected_task_id is not None and event.task_id != expected_task_id:
            raise TransitionAuthorityError("projection attempt does not belong to the command task")
        observed_applied = arguments.get("observed_applied")
        if observed_applied not in {True, False, None}:
            raise TransitionAuthorityError("observed_applied must be true, false, or null")
        adjudication = self.record_observation_and_adjudicate(
            attempt_id=attempt_id,
            observation_kind=str(arguments.get("observation_kind", "reread")),
            observed_applied=observed_applied,
            observed_identity=arguments.get("observed_identity"),
            reread_complete=bool(arguments.get("reread_complete", False)),
            evidence=dict(arguments.get("evidence", {})),
            decided_by="marco",
            decision_reason=f"{route} by {actor}",
            observed_at=recovered_at,
        )
        return {
            "attempt_id": str(attempt_id),
            "projection_event_id": str(event.projection_event_id),
            "adjudication_id": str(adjudication.adjudication_id),
            "outcome": adjudication.outcome,
        }

    def record_drift_and_reproject(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        task_mapping_id: uuid.UUID,
        drift_kind: str,
        external_snapshot: Mapping[str, Any],
        authoritative_snapshot: Mapping[str, Any],
        evidence: Mapping[str, Any],
        detected_at: datetime,
    ) -> tx.ProjectionDriftEvent:
        mapping = self.session.get(tx.TaskProjectionMapping, task_mapping_id)
        epoch = self._active_epoch(generation_id)
        if (
            mapping is None
            or mapping.task_id != task_id
            or mapping.generation_id != generation_id
            or mapping.projection_epoch_id != epoch.projection_epoch_id
            or mapping.state != "active"
        ):
            raise TransitionAuthorityError("drift requires exact active task mapping")
        event = self._record_event(
            generation_id=generation_id,
            execution_id=None,
            task_id=task_id,
            event_type="reproject",
            payload={"drift_kind": drift_kind, "authoritative_snapshot": dict(authoritative_snapshot)},
            source_route="service",
            created_at=detected_at,
        )
        drift = tx.ProjectionDriftEvent(
            drift_event_id=self.uuid_factory(),
            generation_id=generation_id,
            task_id=task_id,
            task_mapping_id=task_mapping_id,
            drift_kind=drift_kind,
            external_snapshot_sha256=sha256_json(dict(external_snapshot)),
            authoritative_snapshot_sha256=sha256_json(dict(authoritative_snapshot)),
            state="reprojected",
            reproject_event_id=event.projection_event_id,
            drift_revision=1,
            evidence=dict(evidence),
            detected_at=detected_at,
            resolved_at=detected_at,
        )
        self.session.add(drift)
        self.session.flush()
        return drift

    def start_reconciliation(
        self,
        *,
        generation_id: uuid.UUID,
        corpus_identity: str,
        expected_items: int,
        started_at: datetime,
    ) -> tx.ProjectionReconciliationRun:
        epoch = self._active_epoch(generation_id)
        row = tx.ProjectionReconciliationRun(
            reconciliation_run_id=self.uuid_factory(),
            generation_id=generation_id,
            projection_epoch_id=epoch.projection_epoch_id,
            corpus_identity=corpus_identity,
            status="running",
            expected_items=expected_items,
            processed_items=0,
            started_at=started_at,
            completed_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_reconciliation_item(
        self,
        *,
        reconciliation_run_id: uuid.UUID,
        item_identity: str,
        entity_kind: str,
        mapping_id: uuid.UUID | None,
        outcome: str,
        evidence: Mapping[str, Any],
        recorded_at: datetime,
    ) -> tx.ProjectionReconciliationItem:
        run = self.session.get(tx.ProjectionReconciliationRun, reconciliation_run_id)
        if run is None or run.status != "running":
            raise TransitionAuthorityError("reconciliation run is not active")
        existing = self.session.scalar(
            select(tx.ProjectionReconciliationItem).where(
                tx.ProjectionReconciliationItem.reconciliation_run_id == reconciliation_run_id,
                tx.ProjectionReconciliationItem.item_identity == item_identity,
            )
        )
        if existing is not None:
            if (
                existing.entity_kind != entity_kind
                or existing.mapping_id != mapping_id
                or existing.outcome != outcome
                or existing.evidence != dict(evidence)
            ):
                raise TransitionAuthorityError("reconciliation item identity conflict")
            return existing
        if run.processed_items >= run.expected_items:
            raise TransitionAuthorityError("reconciliation would exceed expected item count")
        row = tx.ProjectionReconciliationItem(
            reconciliation_item_id=self.uuid_factory(),
            reconciliation_run_id=reconciliation_run_id,
            item_identity=item_identity,
            entity_kind=entity_kind,
            mapping_id=mapping_id,
            outcome=outcome,
            evidence=dict(evidence),
            recorded_at=recorded_at,
        )
        self.session.add(row)
        run.processed_items += 1
        self.session.flush()
        return row

    def complete_reconciliation(
        self, *, reconciliation_run_id: uuid.UUID, completed_at: datetime
    ) -> tx.ProjectionReconciliationRun:
        run = self.session.get(tx.ProjectionReconciliationRun, reconciliation_run_id)
        if run is None or run.status != "running":
            raise TransitionAuthorityError("reconciliation run is not active")
        if run.processed_items != run.expected_items:
            raise TransitionAuthorityError("reconciliation corpus is incomplete")
        blocked = self.session.scalar(
            select(func.count()).select_from(tx.ProjectionReconciliationItem).where(
                tx.ProjectionReconciliationItem.reconciliation_run_id == reconciliation_run_id,
                tx.ProjectionReconciliationItem.outcome.in_(("unknown_external", "blocked")),
            )
        )
        run.status = "blocked" if blocked else "complete"
        run.completed_at = completed_at
        self.session.flush()
        return run

    def unresolved_attempt_id(self, task_id: uuid.UUID) -> uuid.UUID | None:
        value = self.session.scalar(
            select(tx.ProjectionAttempt.attempt_id)
            .join(
                tx.ProjectionOutboxEvent,
                tx.ProjectionOutboxEvent.projection_event_id
                == tx.ProjectionAttempt.projection_event_id,
            )
            .where(
                tx.ProjectionOutboxEvent.task_id == task_id,
                tx.ProjectionOutboxEvent.state.in_(("claimed", "uncertain", "blocked")),
                tx.ProjectionAttempt.state.in_(("dispatched", "uncertain", "blocked")),
            )
            .order_by(
                tx.ProjectionOutboxEvent.aggregate_sequence,
                tx.ProjectionAttempt.attempt_number.desc(),
            )
            .limit(1)
        )
        return value

    def task_freshness(self, task_id: uuid.UUID) -> Mapping[str, Any]:
        latest = self.session.scalar(
            select(tx.ProjectionOutboxEvent)
            .where(tx.ProjectionOutboxEvent.task_id == task_id)
            .order_by(tx.ProjectionOutboxEvent.aggregate_sequence.desc())
            .limit(1)
        )
        if latest is None:
            mapping = self.session.scalar(
                select(tx.TaskProjectionMapping).where(
                    tx.TaskProjectionMapping.task_id == task_id,
                    tx.TaskProjectionMapping.state == "active",
                )
            )
            if mapping is not None:
                return {
                    "state": "imported_mapping",
                    "fresh": True,
                    "mapping_id": str(mapping.mapping_id),
                }
            return {"state": "no_projection_event", "fresh": False}
        epoch = self.session.get(tx.ProjectionEpoch, latest.projection_epoch_id)
        active_epoch = epoch is not None and epoch.status == "active"
        return {
            "state": latest.state,
            "fresh": latest.state == "applied" and active_epoch,
            "projection_event_id": str(latest.projection_event_id),
            "projection_epoch_id": str(latest.projection_epoch_id),
            "aggregate_sequence": latest.aggregate_sequence,
            "created_at": latest.created_at.isoformat(),
            "terminal_at": latest.terminal_at.isoformat() if latest.terminal_at else None,
        }

    def _active_epoch(self, generation_id: uuid.UUID) -> tx.ProjectionEpoch:
        epoch = self.session.scalar(
            select(tx.ProjectionEpoch).where(
                tx.ProjectionEpoch.generation_id == generation_id,
                tx.ProjectionEpoch.status == "active",
            )
        )
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if epoch is None or generation is None or generation.status != "active":
            raise TransitionAuthorityError("active projection epoch is required")
        return epoch
