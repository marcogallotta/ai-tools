"""Stage 5 import, shadow, reconciliation, and projection services.

All services participate in a caller-owned SQLAlchemy transaction. No method
performs network I/O or commits. External workers persist an attempt before an
Asana call and persist exact reread evidence before adjudication.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from .planner import EffectObservation, adjudicate_effect
from .shadow_evidence import compare_evidence
from .workflow import sha256_json


class TransitionAuthorityError(ValueError):
    pass


class ProjectionContentionLost(TransitionAuthorityError):
    pass


@dataclass(frozen=True)
class ProjectionAttemptSnapshot:
    attempt_id: uuid.UUID
    attempt_number: int
    attempt_kind: str
    request_identity: str
    dispatch_identity: str
    external_dispatch_identity: str
    request_payload: Mapping[str, Any]
    intended_external_id: str | None
    retry_generation: int


@dataclass(frozen=True)
class ProjectionClaim:
    event_id: uuid.UUID
    claim_token: uuid.UUID
    claim_revision: int
    claim_expires_at: datetime
    task_id: uuid.UUID
    aggregate_sequence: int
    event_type: str
    payload: Mapping[str, Any]
    idempotency_key: str
    recovery_attempt: ProjectionAttemptSnapshot | None = None

    @property
    def recovery_required(self) -> bool:
        return self.recovery_attempt is not None


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

    @staticmethod
    def _utc_comparable(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _lock_baseline(self, baseline_id: uuid.UUID) -> tx.ShadowBaseline | None:
        statement = select(tx.ShadowBaseline).where(
            tx.ShadowBaseline.shadow_baseline_id == baseline_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _lock_delivery(self, delivery_id: uuid.UUID) -> tx.ShadowDelivery | None:
        statement = select(tx.ShadowDelivery).where(
            tx.ShadowDelivery.delivery_id == delivery_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _lock_gap(self, gap_id: uuid.UUID) -> tx.ShadowGap | None:
        statement = select(tx.ShadowGap).where(tx.ShadowGap.gap_id == gap_id)
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _lock_delivery_path(
        self,
        delivery_id: uuid.UUID,
    ) -> tuple[tx.ShadowBaseline, tx.ShadowDelivery, tx.ShadowEnvelope]:
        baseline_id = self.session.scalar(
            select(tx.ShadowEnvelope.shadow_baseline_id)
            .join(
                tx.ShadowDelivery,
                tx.ShadowDelivery.envelope_id == tx.ShadowEnvelope.envelope_id,
            )
            .where(tx.ShadowDelivery.delivery_id == delivery_id)
        )
        if baseline_id is None:
            raise TransitionAuthorityError("unknown shadow delivery")
        baseline = self._lock_baseline(baseline_id)
        if baseline is None:
            raise TransitionAuthorityError("shadow delivery has no baseline authority")
        delivery = self._lock_delivery(delivery_id)
        if delivery is None:
            raise TransitionAuthorityError("unknown shadow delivery")
        envelope = self.session.get(tx.ShadowEnvelope, delivery.envelope_id)
        if envelope is None or envelope.shadow_baseline_id != baseline.shadow_baseline_id:
            raise TransitionAuthorityError("shadow delivery authority changed while locking")
        return baseline, delivery, envelope

    def _lock_gap_path(
        self,
        gap_id: uuid.UUID,
    ) -> tuple[tx.ShadowBaseline, tx.ShadowDelivery | None, tx.ShadowGap]:
        path = self.session.execute(
            select(tx.ShadowGap.shadow_baseline_id, tx.ShadowGap.envelope_id).where(
                tx.ShadowGap.gap_id == gap_id
            )
        ).one_or_none()
        if path is None:
            raise TransitionAuthorityError("unknown shadow gap")
        baseline = self._lock_baseline(path.shadow_baseline_id)
        if baseline is None:
            raise TransitionAuthorityError("shadow gap has no baseline authority")
        delivery = None
        if path.envelope_id is not None:
            delivery_id = self.session.scalar(
                select(tx.ShadowDelivery.delivery_id).where(
                    tx.ShadowDelivery.envelope_id == path.envelope_id
                )
            )
            if delivery_id is not None:
                delivery = self._lock_delivery(delivery_id)
        gap = self._lock_gap(gap_id)
        if (
            gap is None
            or gap.shadow_baseline_id != baseline.shadow_baseline_id
            or gap.envelope_id != path.envelope_id
            or (delivery is not None and delivery.envelope_id != gap.envelope_id)
        ):
            raise TransitionAuthorityError("shadow gap authority changed while locking")
        return baseline, delivery, gap

    def _assert_delivery_claim(
        self,
        *,
        baseline: tx.ShadowBaseline,
        delivery: tx.ShadowDelivery,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        at: datetime,
    ) -> datetime:
        generation = self.session.get(models.AuthorityGeneration, baseline.generation_id)
        if baseline.status != "open" or generation is None or generation.status != "active":
            raise TransitionAuthorityError("shadow delivery baseline authority is no longer current")
        if (
            delivery.state != "claimed"
            or delivery.claim_token != claim_token
            or delivery.claim_owner != worker_id
            or delivery.delivery_revision != claim_revision
            or delivery.claim_expires_at is None
            or self._utc_comparable(delivery.claim_expires_at) <= self._utc_comparable(at)
        ):
            raise TransitionAuthorityError("shadow delivery settlement claim is stale or expired")
        return delivery.claim_expires_at

    def _settle_delivery_cas(
        self,
        *,
        delivery: tx.ShadowDelivery,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        claim_expires_at: datetime,
        state: str,
        terminal_at: datetime,
        last_error: str | None = None,
    ) -> None:
        result = self.session.execute(
            update(tx.ShadowDelivery)
            .where(
                tx.ShadowDelivery.delivery_id == delivery.delivery_id,
                tx.ShadowDelivery.state == "claimed",
                tx.ShadowDelivery.claim_owner == worker_id,
                tx.ShadowDelivery.claim_token == claim_token,
                tx.ShadowDelivery.delivery_revision == claim_revision,
                tx.ShadowDelivery.claim_expires_at == claim_expires_at,
                tx.ShadowDelivery.claim_expires_at > terminal_at,
            )
            .values(
                state=state,
                claim_owner=None,
                claim_token=None,
                claim_expires_at=None,
                delivery_revision=claim_revision + 1,
                last_error=last_error,
                terminal_at=terminal_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise TransitionAuthorityError("shadow delivery settlement lost current claim authority")
        self.session.expire(delivery)

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
        rollout_sequence: int | None = None,
        source_authority_generation: str | None = None,
        source_execution_identity: str | None = None,
        principal: Mapping[str, Any] | None = None,
        source_pre_state: Mapping[str, Any] | None = None,
        pinned_inputs: Mapping[str, Any] | None = None,
        source_effects: Mapping[str, Any] | None = None,
        capture_qualification: str = "legacy",
        envelope_schema_version: int = 1,
    ) -> tx.ShadowEnvelope:
        baseline = self._lock_baseline(shadow_baseline_id)
        if baseline is None or baseline.status != "open":
            raise TransitionAuthorityError("shadow baseline is not open")
        target_generation = self.session.get(models.AuthorityGeneration, baseline.generation_id)
        if target_generation is None or target_generation.status != "active":
            raise TransitionAuthorityError("shadow baseline target generation is stale")
        if source_authority_generation is None:
            raise TransitionAuthorityError("shadow envelope source generation is required")
        if source_authority_generation != baseline.source_generation_identity:
            raise TransitionAuthorityError("shadow envelope source generation does not match baseline")
        if capture_qualification in {"execute", "capture_only"} and (
            rollout_sequence is None or rollout_sequence <= 0
        ):
            raise TransitionAuthorityError("dark-launch envelope rollout sequence is required")
        existing = self.session.scalar(
            select(tx.ShadowEnvelope).where(
                tx.ShadowEnvelope.shadow_baseline_id == shadow_baseline_id,
                tx.ShadowEnvelope.source_request_identity == source_request_identity,
            )
        )
        input_payload, outcome_payload = dict(canonical_input), dict(source_outcome)
        pre_payload = None if source_pre_state is None else dict(source_pre_state)
        post_payload = dict(source_post_state)
        principal_payload = None if principal is None else dict(principal)
        pinned_payload = None if pinned_inputs is None else dict(pinned_inputs)
        effects_payload = None if source_effects is None else dict(source_effects)
        if existing is not None:
            if (
                existing.command_name != command_name
                or existing.canonical_input_sha256 != sha256_json(input_payload)
                or existing.source_outcome_sha256 != sha256_json(outcome_payload)
                or existing.source_post_state != post_payload
                or existing.rollout_sequence != rollout_sequence
                or existing.source_authority_generation != source_authority_generation
                or existing.source_execution_identity != source_execution_identity
                or existing.principal != principal_payload
                or existing.source_pre_state != pre_payload
                or existing.pinned_inputs != pinned_payload
                or existing.source_effects != effects_payload
                or existing.capture_qualification != capture_qualification
                or existing.envelope_schema_version != envelope_schema_version
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
            source_post_state=post_payload,
            rollout_sequence=rollout_sequence,
            source_authority_generation=source_authority_generation,
            source_execution_identity=source_execution_identity,
            principal=principal_payload,
            source_pre_state=pre_payload,
            source_pre_state_sha256=None if pre_payload is None else sha256_json(pre_payload),
            pinned_inputs=pinned_payload,
            source_effects=effects_payload,
            capture_qualification=capture_qualification,
            source_post_state_sha256=sha256_json(post_payload),
            envelope_schema_version=envelope_schema_version,
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
        shadow_baseline_id: uuid.UUID | None = None,
    ) -> tx.ShadowDelivery | None:
        query = (
            select(tx.ShadowDelivery, tx.ShadowEnvelope)
            .join(
                tx.ShadowEnvelope,
                tx.ShadowEnvelope.envelope_id == tx.ShadowDelivery.envelope_id,
            )
            .join(
                tx.ShadowBaseline,
                tx.ShadowBaseline.shadow_baseline_id == tx.ShadowEnvelope.shadow_baseline_id,
            )
            .join(
                models.AuthorityGeneration,
                models.AuthorityGeneration.generation_id == tx.ShadowBaseline.generation_id,
            )
            .where(
                tx.ShadowBaseline.status == "open",
                models.AuthorityGeneration.status == "active",
            )
        )
        if shadow_baseline_id is not None:
            query = query.where(tx.ShadowEnvelope.shadow_baseline_id == shadow_baseline_id)
        candidates = self.session.execute(
            query.where(
                (tx.ShadowDelivery.state == "pending")
                | (
                    (tx.ShadowDelivery.state == "claimed")
                    & (tx.ShadowDelivery.claim_expires_at <= now)
                )
            ).order_by(
                tx.ShadowEnvelope.rollout_sequence.is_(None),
                tx.ShadowEnvelope.rollout_sequence,
                tx.ShadowDelivery.created_at,
                tx.ShadowDelivery.delivery_id,
            )
        ).all()
        for candidate, envelope in candidates:
            baseline = self._lock_baseline(envelope.shadow_baseline_id)
            if baseline is None or baseline.status != "open":
                continue
            generation = self.session.get(models.AuthorityGeneration, baseline.generation_id)
            if generation is None or generation.status != "active":
                continue
            row = self._lock_delivery(candidate.delivery_id)
            if row is None or row.envelope_id != envelope.envelope_id:
                continue
            if row.state == "pending":
                pass
            elif (
                row.state == "claimed"
                and row.claim_expires_at is not None
                and self._utc_comparable(row.claim_expires_at) <= self._utc_comparable(now)
            ):
                pass
            else:
                continue
            if envelope.rollout_sequence is not None:
                blockers = int(self.session.scalar(
                    select(func.count())
                    .select_from(tx.ShadowDelivery)
                    .join(
                        tx.ShadowEnvelope,
                        tx.ShadowEnvelope.envelope_id == tx.ShadowDelivery.envelope_id,
                    )
                    .where(
                        tx.ShadowEnvelope.shadow_baseline_id == envelope.shadow_baseline_id,
                        tx.ShadowEnvelope.rollout_sequence.is_not(None),
                        tx.ShadowEnvelope.rollout_sequence < envelope.rollout_sequence,
                        tx.ShadowDelivery.state != "delivered",
                    )
                ) or 0)
                if blockers:
                    continue
            revision = row.delivery_revision
            prior_state = row.state
            predicate = [
                tx.ShadowDelivery.delivery_id == row.delivery_id,
                tx.ShadowDelivery.delivery_revision == revision,
                tx.ShadowDelivery.state == prior_state,
            ]
            if prior_state == "claimed":
                predicate.extend(
                    (
                        tx.ShadowDelivery.claim_owner == row.claim_owner,
                        tx.ShadowDelivery.claim_token == row.claim_token,
                        tx.ShadowDelivery.claim_expires_at == row.claim_expires_at,
                        tx.ShadowDelivery.claim_expires_at <= now,
                    )
                )
            result = self.session.execute(
                update(tx.ShadowDelivery)
                .where(*predicate)
                .values(
                    state="claimed",
                    claim_owner=worker_id,
                    claim_token=claim_token,
                    claim_expires_at=now + ttl,
                    delivery_revision=revision + 1,
                    attempts=row.attempts + 1,
                    last_error=None,
                )
                .execution_options(synchronize_session=False)
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
        claim_revision: int,
        worker_id: str,
        target_result: Mapping[str, Any],
        comparator_release: str,
        compared_at: datetime,
        semantic_normalizer: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> tx.ShadowComparison:
        baseline, delivery, envelope = self._lock_delivery_path(delivery_id)
        claim_expires_at = self._assert_delivery_claim(
            baseline=baseline,
            delivery=delivery,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            at=compared_at,
        )
        target = dict(target_result)
        if target.get("evidence_schema_version") is not None:
            parity, differences = compare_evidence(
                source_outcome=envelope.source_outcome,
                source_pre_state=envelope.source_pre_state,
                source_post_state=envelope.source_post_state,
                target_payload=target,
            )
        elif envelope.source_outcome == target:
            parity, differences = "exact", []
        elif semantic_normalizer and semantic_normalizer(envelope.source_outcome) == semantic_normalizer(target):
            parity, differences = "semantic", []
        else:
            parity = "mismatch"
            differences = [{"axis": "response", "source": envelope.source_outcome, "target": target}]
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
        self._settle_delivery_cas(
            delivery=delivery,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            claim_expires_at=claim_expires_at,
            state="delivered",
            terminal_at=compared_at,
        )
        self.session.add(comparison)
        if parity in {"mismatch", "gap"}:
            kind = "mismatch" if parity == "mismatch" else "uncomparable"
            self._open_gap(
                baseline_id=envelope.shadow_baseline_id,
                envelope_id=envelope.envelope_id,
                identity=f"{kind}:{envelope.source_request_identity}",
                kind=kind,
                details={"differences": differences},
                at=compared_at,
            )
        self.session.flush()
        return comparison

    def skip_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        reason: str,
        comparator_release: str,
        completed_at: datetime,
    ) -> tx.ShadowComparison:
        """Settle a deliberately capture-only envelope as an explicit gap."""
        baseline, delivery, envelope = self._lock_delivery_path(delivery_id)
        claim_expires_at = self._assert_delivery_claim(
            baseline=baseline,
            delivery=delivery,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            at=completed_at,
        )
        target = {"shadow_execution": "skipped", "reason": reason}
        comparison = tx.ShadowComparison(
            comparison_id=self.uuid_factory(),
            envelope_id=envelope.envelope_id,
            target_result=target,
            target_result_sha256=sha256_json(target),
            parity_class="gap",
            differences=[{"reason": reason}],
            comparator_release=comparator_release,
            compared_at=completed_at,
        )
        self._settle_delivery_cas(
            delivery=delivery,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            claim_expires_at=claim_expires_at,
            state="delivered",
            terminal_at=completed_at,
        )
        self.session.add(comparison)
        self._open_gap(
            baseline_id=envelope.shadow_baseline_id,
            envelope_id=envelope.envelope_id,
            identity=f"uncomparable:{envelope.source_request_identity}",
            kind="uncomparable",
            details={"reason": reason},
            at=completed_at,
        )
        self.session.flush()
        return comparison

    def void_failed_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        reason: str,
        comparator_release: str,
        completed_at: datetime,
    ) -> tx.ShadowComparison:
        """Operator-settle one terminal failed delivery as abandoned evaluation evidence."""
        baseline, delivery, envelope = self._lock_delivery_path(delivery_id)
        generation = self.session.get(models.AuthorityGeneration, baseline.generation_id)
        if baseline.status != "open" or generation is None or generation.status != "active":
            raise TransitionAuthorityError("shadow delivery baseline authority is no longer current")
        reason = reason.strip()
        if not reason:
            raise TransitionAuthorityError("shadow delivery operator void reason is required")
        if delivery.state != "failed":
            raise TransitionAuthorityError(
                "shadow delivery operator void requires terminal failed state; "
                f"current state is {delivery.state}"
            )

        failed_revision = delivery.delivery_revision
        failed_at = delivery.terminal_at
        voided_revision = failed_revision + 1
        gap_identity = (
            f"operator_voided:delivery:{delivery.delivery_id}:revision:{voided_revision}"
        )
        audit = {
            "audit_kind": "operator_voided",
            "reason": reason,
            "evaluation_abandoned": True,
            "failed_delivery_revision": failed_revision,
            "voided_delivery_revision": voided_revision,
            "failed_at": None if failed_at is None else failed_at.isoformat(),
            "failed_error": delivery.last_error,
            "gap_identity": gap_identity,
            "source_request_identity": envelope.source_request_identity,
        }
        target = {
            "shadow_execution": "not_evaluated",
            "settlement": "operator_voided",
            "evaluation_abandoned": True,
        }
        comparison = tx.ShadowComparison(
            comparison_id=self.uuid_factory(),
            envelope_id=envelope.envelope_id,
            target_result=target,
            target_result_sha256=sha256_json(target),
            parity_class="gap",
            differences=[audit],
            comparator_release=comparator_release,
            compared_at=completed_at,
        )
        result = self.session.execute(
            update(tx.ShadowDelivery)
            .where(
                tx.ShadowDelivery.delivery_id == delivery.delivery_id,
                tx.ShadowDelivery.envelope_id == envelope.envelope_id,
                tx.ShadowDelivery.state == "failed",
                tx.ShadowDelivery.delivery_revision == failed_revision,
                tx.ShadowDelivery.claim_owner.is_(None),
                tx.ShadowDelivery.claim_token.is_(None),
                tx.ShadowDelivery.claim_expires_at.is_(None),
                tx.ShadowDelivery.terminal_at == failed_at,
            )
            .values(
                state="delivered",
                delivery_revision=voided_revision,
                terminal_at=completed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise TransitionAuthorityError(
                "shadow delivery operator void lost current failed authority"
            )
        self.session.expire(delivery)
        self.session.add(comparison)
        self._open_gap(
            baseline_id=envelope.shadow_baseline_id,
            envelope_id=envelope.envelope_id,
            identity=gap_identity,
            kind="delivery_failure",
            details=audit,
            at=completed_at,
        )
        original_gap_identity = f"delivery:{envelope.source_request_identity}:revision:{failed_revision}"
        original_gap = self.session.scalar(
            select(tx.ShadowGap).where(
                tx.ShadowGap.shadow_baseline_id == envelope.shadow_baseline_id,
                tx.ShadowGap.gap_identity == original_gap_identity,
                tx.ShadowGap.state == "open",
            )
        )
        if original_gap is not None:
            original_gap.state = "resolved"
            original_gap.resolution = {
                "delivery_outcome": "operator_voided",
                "reason": reason,
                "void_comparison_id": str(comparison.comparison_id),
                "void_gap_identity": gap_identity,
            }
            original_gap.resolved_at = completed_at
        self.session.flush()
        return comparison

    def fail_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        error: str,
        failed_at: datetime,
    ) -> tx.ShadowGap:
        baseline, delivery, envelope = self._lock_delivery_path(delivery_id)
        claim_expires_at = self._assert_delivery_claim(
            baseline=baseline,
            delivery=delivery,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            at=failed_at,
        )
        self._settle_delivery_cas(
            delivery=delivery,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            claim_expires_at=claim_expires_at,
            state="failed",
            terminal_at=failed_at,
            last_error=error,
        )
        gap = self._open_gap(
            baseline_id=envelope.shadow_baseline_id,
            envelope_id=envelope.envelope_id,
            identity=(
                f"delivery:{envelope.source_request_identity}:"
                f"revision:{claim_revision + 1}"
            ),
            kind="delivery_failure",
            details={
                "error": error,
                "failed_delivery_revision": claim_revision + 1,
            },
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
            if (
                existing.envelope_id != envelope_id
                or existing.gap_kind != kind
                or existing.details != dict(details)
            ):
                raise TransitionAuthorityError("shadow gap identity conflict")
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
        baseline = self._lock_baseline(baseline_id)
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
        baseline, delivery, gap = self._lock_gap_path(gap_id)
        generation = self.session.get(models.AuthorityGeneration, baseline.generation_id)
        if baseline.status != "open" or generation is None or generation.status != "active":
            raise TransitionAuthorityError("shadow gap recovery authority is no longer current")
        if gap.state != "open":
            raise TransitionAuthorityError("shadow gap is not open")
        resolution_payload = dict(resolution)
        if gap.gap_kind == "uncomparable" and gap.envelope_id is not None and not waived:
            envelope = self.session.get(tx.ShadowEnvelope, gap.envelope_id)
            if (
                envelope is not None
                and envelope.command_name == "create"
                and envelope.capture_qualification == "capture_only"
                and gap.details.get("reason") == "dark-launch treatment is capture_only"
            ):
                raise TransitionAuthorityError(
                    "capture-only create evidence is immutable and cannot be requeued; "
                    "a superseding replay mechanism is required"
                )
        if gap.gap_kind == "delivery_failure" and gap.envelope_id is not None and not waived:
            if resolution_payload.get("delivery_outcome") != "not_applied":
                raise TransitionAuthorityError(
                    "shadow delivery recovery remains uncertain without not-applied proof"
                )
            failed_revision = gap.details.get("failed_delivery_revision")
            if not isinstance(failed_revision, int) or failed_revision <= 0:
                raise TransitionAuthorityError(
                    "shadow delivery recovery lacks exact failed revision evidence"
                )
            if delivery is None or delivery.state != "failed":
                raise TransitionAuthorityError("shadow delivery is not failed")
            terminal_at = delivery.terminal_at
            result = self.session.execute(
                update(tx.ShadowDelivery)
                .where(
                    tx.ShadowDelivery.delivery_id == delivery.delivery_id,
                    tx.ShadowDelivery.envelope_id == gap.envelope_id,
                    tx.ShadowDelivery.state == "failed",
                    tx.ShadowDelivery.delivery_revision == failed_revision,
                    tx.ShadowDelivery.claim_owner.is_(None),
                    tx.ShadowDelivery.claim_token.is_(None),
                    tx.ShadowDelivery.claim_expires_at.is_(None),
                    tx.ShadowDelivery.terminal_at == terminal_at,
                )
                .values(
                    state="pending",
                    terminal_at=None,
                    delivery_revision=failed_revision + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise TransitionAuthorityError(
                    "shadow delivery recovery lost current failed authority"
                )
            self.session.expire(delivery)
        gap_revision = gap.gap_revision
        result = self.session.execute(
            update(tx.ShadowGap)
            .where(
                tx.ShadowGap.gap_id == gap.gap_id,
                tx.ShadowGap.shadow_baseline_id == baseline.shadow_baseline_id,
                tx.ShadowGap.state == "open",
                tx.ShadowGap.gap_revision == gap_revision,
                tx.ShadowGap.gap_kind == gap.gap_kind,
                tx.ShadowGap.envelope_id == gap.envelope_id,
            )
            .values(
                state="waived" if waived else "resolved",
                resolution=resolution_payload,
                gap_revision=gap_revision + 1,
                resolved_at=resolved_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise TransitionAuthorityError("shadow gap recovery lost current authority")
        self.session.expire(gap)
        self.session.flush()
        return gap

    def close_baseline(self, *, baseline_id: uuid.UUID, closed_at: datetime) -> tx.ShadowBaseline:
        baseline = self._lock_baseline(baseline_id)
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

    def disqualify_baseline(
        self, *, baseline_id: uuid.UUID, reason: str, at: datetime
    ) -> tx.ShadowBaseline:
        baseline = self._lock_baseline(baseline_id)
        if baseline is None or baseline.status != "open":
            raise TransitionAuthorityError("shadow baseline is not open")
        baseline.status = "disqualified"
        baseline.disqualification_reason = reason
        baseline.terminal_at = at
        self.session.flush()
        return baseline

    def disqualify_stale_baselines(
        self, *, active_generation_id: uuid.UUID, reason: str, at: datetime
    ) -> int:
        statement = (
            select(tx.ShadowBaseline)
            .where(
                tx.ShadowBaseline.status == "open",
                tx.ShadowBaseline.generation_id != active_generation_id,
            )
            .order_by(tx.ShadowBaseline.shadow_baseline_id)
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        rows = self.session.scalars(statement).all()
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

    def _lock_epoch(
        self,
        projection_epoch_id: uuid.UUID,
        *,
        shared: bool,
    ) -> tx.ProjectionEpoch | None:
        statement = select(tx.ProjectionEpoch).where(
            tx.ProjectionEpoch.projection_epoch_id == projection_epoch_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=shared)
            statement = statement.execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _active_epoch_for_generation(
        self,
        generation_id: uuid.UUID,
        *,
        shared: bool,
    ) -> tx.ProjectionEpoch | None:
        statement = select(tx.ProjectionEpoch).where(
            tx.ProjectionEpoch.generation_id == generation_id,
            tx.ProjectionEpoch.status == "active",
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=shared)
            statement = statement.execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _event_epoch_id(self, event_id: uuid.UUID) -> uuid.UUID | None:
        return self.session.scalar(
            select(tx.ProjectionOutboxEvent.projection_epoch_id).where(
                tx.ProjectionOutboxEvent.projection_event_id == event_id
            )
        )

    def _lock_event_path(
        self,
        event_id: uuid.UUID,
    ) -> tuple[tx.ProjectionEpoch, tx.ProjectionOutboxEvent]:
        projection_epoch_id = self._event_epoch_id(event_id)
        if projection_epoch_id is None:
            raise TransitionAuthorityError("unknown projection event")
        epoch = self._lock_epoch(projection_epoch_id, shared=True)
        if epoch is None:
            raise TransitionAuthorityError("projection event has no epoch authority")
        event = self._lock_event(event_id)
        if event.projection_epoch_id != epoch.projection_epoch_id:
            raise TransitionAuthorityError("projection event epoch changed while locking")
        return epoch, event

    def _lock_attempt(
        self,
        attempt_id: uuid.UUID,
    ) -> tx.ProjectionAttempt | None:
        statement = select(tx.ProjectionAttempt).where(
            tx.ProjectionAttempt.attempt_id == attempt_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
            statement = statement.execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _lock_attempt_path(
        self,
        attempt_id: uuid.UUID,
    ) -> tuple[tx.ProjectionEpoch, tx.ProjectionOutboxEvent, tx.ProjectionAttempt]:
        event_id = self.session.scalar(
            select(tx.ProjectionAttempt.projection_event_id).where(
                tx.ProjectionAttempt.attempt_id == attempt_id
            )
        )
        if event_id is None:
            raise TransitionAuthorityError("unknown projection attempt")
        epoch, event = self._lock_event_path(event_id)
        attempt = self._lock_attempt(attempt_id)
        if attempt is None or attempt.projection_event_id != event.projection_event_id:
            raise TransitionAuthorityError("projection attempt authority changed while locking")
        return epoch, event, attempt

    def _latest_attempt(
        self,
        event_id: uuid.UUID,
        *,
        for_update: bool,
    ) -> tx.ProjectionAttempt | None:
        statement = (
            select(tx.ProjectionAttempt)
            .where(tx.ProjectionAttempt.projection_event_id == event_id)
            .order_by(tx.ProjectionAttempt.attempt_number.desc())
            .limit(1)
        )
        if for_update and self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
            statement = statement.execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _claim_candidates(self, *, now: datetime) -> list[tx.ProjectionOutboxEvent]:
        return self.session.scalars(
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
                tx.ProjectionEpoch.external_effects_enabled.is_(True),
                tx.ProjectionOutboxEvent.origin == "live",
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

    def activate_epoch(
        self,
        *,
        generation_id: uuid.UUID,
        activation_reason: str,
        created_at: datetime,
        external_effects_enabled: bool = False,
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
            if active.external_effects_enabled != external_effects_enabled:
                raise TransitionAuthorityError("active projection epoch effect mode does not match")
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
            external_effects_enabled=external_effects_enabled,
            created_at=created_at,
            retired_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row


    def set_external_effects_enabled(
        self,
        *,
        projection_epoch_id: uuid.UUID,
        enabled: bool,
        reason: str,
    ) -> tx.ProjectionEpoch:
        """Explicitly gate whether projection workers may claim this epoch.

        Dark-launch epochs start disabled. Enabling is a distinct operator
        decision and cannot be inferred from epoch activation.
        """

        epoch = self._lock_epoch(projection_epoch_id, shared=False)
        if epoch is None or epoch.status != "active":
            raise TransitionAuthorityError("projection epoch is not active")
        if not str(reason).strip():
            raise TransitionAuthorityError("projection effect-mode change requires a reason")
        epoch.external_effects_enabled = bool(enabled)
        self.session.flush()
        return epoch

    def retire_epoch(self, *, projection_epoch_id: uuid.UUID, retired_at: datetime) -> None:
        epoch = self._lock_epoch(projection_epoch_id, shared=False)
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
        event_statement = select(tx.ProjectionOutboxEvent).where(
            tx.ProjectionOutboxEvent.projection_epoch_id == projection_epoch_id,
            tx.ProjectionOutboxEvent.state.not_in(("applied", "superseded")),
        )
        if self.session.get_bind().dialect.name == "postgresql":
            event_statement = event_statement.with_for_update().execution_options(
                populate_existing=True
            )
        events = self.session.scalars(event_statement).all()
        for event in events:
            event.state = "superseded"
            event.claim_owner = None
            event.claim_token = None
            event.claim_expires_at = None
            event.outbox_revision += 1
            event.terminal_at = retired_at
            attempt_statement = select(tx.ProjectionAttempt).where(
                tx.ProjectionAttempt.projection_event_id == event.projection_event_id,
                tx.ProjectionAttempt.state.in_(("dispatched", "uncertain", "blocked")),
            )
            if self.session.get_bind().dialect.name == "postgresql":
                attempt_statement = attempt_statement.with_for_update().execution_options(
                    populate_existing=True
                )
            attempts = self.session.scalars(attempt_statement).all()
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
        origin: str = "live",
        created_at: datetime,
    ) -> uuid.UUID:
        return self._record_event(
            generation_id=generation_id,
            execution_id=execution_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            source_route="command",
            origin=origin,
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
        origin: str,
        created_at: datetime,
    ) -> tx.ProjectionOutboxEvent:
        epoch = self._active_epoch_for_generation(generation_id, shared=True)
        if epoch is None:
            raise TransitionAuthorityError("no active projection epoch")
        if origin not in {"live", "shadow"}:
            raise TransitionAuthorityError("projection origin must be live or shadow")
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
            if existing.origin != origin:
                raise TransitionAuthorityError("projection event origin conflict")
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
            origin=origin,
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

    def _lock_event(self, event_id: uuid.UUID) -> tx.ProjectionOutboxEvent:
        statement = select(tx.ProjectionOutboxEvent).where(
            tx.ProjectionOutboxEvent.projection_event_id == event_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        event = self.session.scalar(statement.execution_options(populate_existing=True))
        if event is None:
            raise TransitionAuthorityError("unknown projection event")
        return event

    @staticmethod
    def _utc_comparable(value: datetime) -> datetime:
        # SQLite returns timezone-aware columns as naive values. Public callers
        # provide aware timestamps; normalize only for durable lease comparison.
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _assert_worker_claim(
        cls,
        *,
        event: tx.ProjectionOutboxEvent,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        at: datetime,
    ) -> None:
        if (
            event.state != "claimed"
            or event.claim_token != claim_token
            or event.claim_owner != worker_id
            or event.outbox_revision != claim_revision
            or event.claim_expires_at is None
            or cls._utc_comparable(event.claim_expires_at) <= cls._utc_comparable(at)
        ):
            raise TransitionAuthorityError("projection settlement claim is stale or expired")

    def _terminalize_unobserved_attempt(
        self,
        *,
        attempt: tx.ProjectionAttempt,
        event: tx.ProjectionOutboxEvent,
        observed_at: datetime,
        reason: str,
    ) -> None:
        if attempt.state != "dispatched":
            raise TransitionAuthorityError("only an active attempt can be terminalized")
        evidence = {
            "external_observation": {
                "source": "unavailable",
                "operation": event.event_type,
                "reason": "claim_expired_before_settlement",
            },
            "prior_dispatch_identity": attempt.dispatch_identity,
        }
        observation = tx.ProjectionObservation(
            observation_id=self.uuid_factory(),
            attempt_id=attempt.attempt_id,
            observation_sequence=1,
            observation_kind="preflight",
            observed_applied=None,
            observed_identity=None,
            reread_complete=False,
            evidence=evidence,
            evidence_sha256=sha256_json(evidence),
            observed_at=observed_at,
        )
        self.session.add(observation)
        self.session.flush()
        self.session.add(
            tx.ProjectionAdjudication(
                adjudication_id=self.uuid_factory(),
                attempt_id=attempt.attempt_id,
                observation_id=observation.observation_id,
                adjudication_sequence=1,
                outcome="uncertain",
                decided_by="automatic",
                decision_reason=reason,
                decided_at=observed_at,
            )
        )
        attempt.state = "uncertain"
        attempt.terminal_at = observed_at
        self.session.flush()

    @staticmethod
    def _reproject_state_identity(event: tx.ProjectionOutboxEvent) -> str | None:
        authoritative_snapshot = event.intent_payload.get("authoritative_snapshot")
        if not isinstance(authoritative_snapshot, Mapping):
            return None
        return sha256_json(dict(authoritative_snapshot))

    @staticmethod
    def _is_independent_external_observation(
        *,
        event: tx.ProjectionOutboxEvent,
        attempt: tx.ProjectionAttempt,
        observation_kind: str,
        observed_applied: bool | None,
        observed_identity: str | None,
        evidence: Mapping[str, Any],
    ) -> bool:
        fact = evidence.get("external_observation")
        if not isinstance(fact, Mapping) or fact.get("operation") != event.event_type:
            return False
        source = fact.get("source")
        if event.event_type == "create_task":
            return (
                observation_kind == "marker_search"
                and source == "external_marker_search"
                and fact.get("correlation_marker")
                == event.intent_payload.get("correlation_marker")
            )
        if observation_kind not in {"reread", "drift_scan"}:
            return False
        if source not in {"external_reread", "external_drift_scan"}:
            return False
        observed_external_id = str(fact.get("observed_external_id") or "").strip()
        if not observed_external_id:
            return False
        if (
            attempt.intended_external_id is not None
            and observed_external_id != attempt.intended_external_id
        ):
            return False
        if observed_applied is False:
            return fact.get("observed_absent") is True
        if observed_applied is not True or not str(observed_identity or "").strip():
            return False
        if event.event_type == "reproject":
            intended_state_identity = ProjectionService._reproject_state_identity(event)
            return (
                intended_state_identity is not None
                and observed_identity == intended_state_identity
                and fact.get("observed_reproject_state_identity") == intended_state_identity
            )
        identity_field = {
            "update_task_document": "observed_document_identity",
            "move_task": "observed_membership_identity",
            "set_completion": "observed_completion_identity",
        }.get(event.event_type)
        return (
            identity_field is not None
            and fact.get(identity_field) == observed_identity
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> ProjectionClaim | None:
        for candidate in self._claim_candidates(now=now):
            epoch = self._lock_epoch(candidate.projection_epoch_id, shared=True)
            if (
                epoch is None
                or epoch.status != "active"
                or not epoch.external_effects_enabled
            ):
                continue
            generation = self.session.get(
                models.AuthorityGeneration,
                candidate.generation_id,
                populate_existing=True,
            )
            if generation is None or generation.status != "active":
                continue
            event = self._lock_event(candidate.projection_event_id)
            if (
                event.projection_epoch_id != epoch.projection_epoch_id
                or event.origin != "live"
                or event.generation_id != generation.generation_id
            ):
                continue
            if event.state == "pending":
                if event.claim_expires_at is not None:
                    continue
            elif event.state == "claimed":
                if (
                    event.claim_expires_at is None
                    or self._utc_comparable(event.claim_expires_at)
                    > self._utc_comparable(now)
                ):
                    continue
            else:
                continue
            blockers = self.session.scalar(
                select(func.count()).select_from(tx.ProjectionOutboxEvent).where(
                    tx.ProjectionOutboxEvent.generation_id == event.generation_id,
                    tx.ProjectionOutboxEvent.task_id == event.task_id,
                    tx.ProjectionOutboxEvent.aggregate_sequence < event.aggregate_sequence,
                    tx.ProjectionOutboxEvent.origin == "live",
                    tx.ProjectionOutboxEvent.state.not_in(("applied", "superseded")),
                )
            )
            if blockers:
                continue
            token = self.uuid_factory()
            claim_expiry = now + ttl
            event.state = "claimed"
            event.claim_owner = worker_id
            event.claim_token = token
            event.claim_expires_at = claim_expiry
            event.outbox_revision += 1
            event.terminal_at = None
            self.session.flush()
            latest = self._latest_attempt(
                event.projection_event_id,
                for_update=False,
            )
            recovery = None
            if latest is not None and latest.state == "dispatched":
                external_dispatch_identity = latest.dispatch_identity
                if latest.attempt_kind == "recovery":
                    predecessor = self.session.get(
                        tx.ProjectionAttempt, latest.predecessor_attempt_id
                    )
                    if predecessor is None or predecessor.attempt_kind != "dispatch":
                        raise TransitionAuthorityError(
                            "active recovery attempt lacks its immutable dispatch predecessor"
                        )
                    external_dispatch_identity = predecessor.dispatch_identity
                recovery = ProjectionAttemptSnapshot(
                    attempt_id=latest.attempt_id,
                    attempt_number=latest.attempt_number,
                    attempt_kind=latest.attempt_kind,
                    request_identity=latest.request_identity,
                    dispatch_identity=latest.dispatch_identity,
                    external_dispatch_identity=external_dispatch_identity,
                    request_payload=dict(latest.request_payload),
                    intended_external_id=latest.intended_external_id,
                    retry_generation=latest.retry_generation,
                )
            return ProjectionClaim(
                event.projection_event_id,
                token,
                event.outbox_revision,
                claim_expiry,
                event.task_id,
                event.aggregate_sequence,
                event.event_type,
                dict(event.intent_payload),
                event.idempotency_key,
                recovery,
            )
        return None

    def begin_attempt(
        self,
        *,
        event_id: uuid.UUID,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        request_identity: str,
        request_payload: Mapping[str, Any],
        intended_external_id: str | None,
        started_at: datetime,
    ) -> tx.ProjectionAttempt:
        epoch, event = self._lock_event_path(event_id)
        self._assert_worker_claim(
            event=event,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            at=started_at,
        )
        generation = self.session.get(models.AuthorityGeneration, event.generation_id)
        if (
            epoch.status != "active"
            or not epoch.external_effects_enabled
            or generation is None
            or generation.status != "active"
        ):
            raise TransitionAuthorityError("stale projection epoch cannot dispatch an attempt")
        latest = self._latest_attempt(event_id, for_update=True)
        if latest is not None and latest.state == "dispatched":
            raise TransitionAuthorityError(
                "projection recovery observation is required before another dispatch"
            )
        if latest is not None and latest.state == "confirmed":
            raise TransitionAuthorityError("confirmed projection event cannot dispatch again")
        payload = dict(request_payload)
        number = (latest.attempt_number if latest is not None else 0) + 1
        retry_generation = (latest.retry_generation if latest is not None else 0) + 1
        dispatch_identity = sha256_json(
            {
                "projection_event_id": str(event_id),
                "request_identity": request_identity,
                "retry_generation": retry_generation,
            }
        )
        row = tx.ProjectionAttempt(
            attempt_id=self.uuid_factory(),
            projection_event_id=event_id,
            attempt_number=number,
            attempt_kind="dispatch",
            predecessor_attempt_id=None,
            worker_id=worker_id,
            request_identity=request_identity,
            dispatch_identity=dispatch_identity,
            retry_generation=retry_generation,
            dispatch_claim_token=claim_token,
            dispatch_claim_revision=claim_revision,
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

    def begin_recovery_attempt(
        self,
        *,
        event_id: uuid.UUID,
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
        prior_attempt_id: uuid.UUID,
        started_at: datetime,
    ) -> tx.ProjectionAttempt:
        _epoch, event = self._lock_event_path(event_id)
        self._assert_worker_claim(
            event=event,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            at=started_at,
        )
        prior = self._lock_attempt(prior_attempt_id)
        if (
            prior is None
            or prior.projection_event_id != event_id
            or prior.state != "dispatched"
        ):
            raise TransitionAuthorityError("projection recovery requires the exact active attempt")
        dispatch = prior
        if prior.attempt_kind == "recovery":
            dispatch = self._lock_attempt(prior.predecessor_attempt_id)
            if dispatch is None or dispatch.attempt_kind != "dispatch":
                raise TransitionAuthorityError(
                    "active recovery attempt lacks its immutable dispatch predecessor"
                )
        self._terminalize_unobserved_attempt(
            attempt=prior,
            event=event,
            observed_at=started_at,
            reason=(
                "dispatch ownership expired before settlement; external outcome requires observation"
                if prior.attempt_kind == "dispatch"
                else "recovery ownership expired before settlement; external outcome remains unresolved"
            ),
        )
        number = prior.attempt_number + 1
        dispatch_identity = sha256_json(
            {
                "projection_event_id": str(event_id),
                "predecessor_attempt_id": str(dispatch.attempt_id),
                "superseded_recovery_attempt_id": (
                    str(prior.attempt_id) if prior.attempt_kind == "recovery" else None
                ),
                "claim_revision": claim_revision,
                "attempt_kind": "recovery",
            }
        )
        row = tx.ProjectionAttempt(
            attempt_id=self.uuid_factory(),
            projection_event_id=event_id,
            attempt_number=number,
            attempt_kind="recovery",
            predecessor_attempt_id=dispatch.attempt_id,
            worker_id=worker_id,
            request_identity=dispatch.request_identity,
            dispatch_identity=dispatch_identity,
            retry_generation=dispatch.retry_generation,
            dispatch_claim_token=None,
            dispatch_claim_revision=None,
            intended_external_id=dispatch.intended_external_id,
            request_payload=dict(dispatch.request_payload),
            request_sha256=dispatch.request_sha256,
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
        claim_token: uuid.UUID | None = None,
        claim_revision: int | None = None,
        worker_id: str | None = None,
    ) -> tx.ProjectionAdjudication:
        _epoch, event, attempt = self._lock_attempt_path(attempt_id)
        if attempt.state != "dispatched":
            raise TransitionAuthorityError("projection attempt is not active")
        if decided_by == "automatic":
            if claim_token is None or claim_revision is None or worker_id is None:
                raise TransitionAuthorityError("automatic settlement requires current claim authority")
            self._assert_worker_claim(
                event=event,
                claim_token=claim_token,
                claim_revision=claim_revision,
                worker_id=worker_id,
                at=observed_at,
            )
            if attempt.worker_id != worker_id:
                raise TransitionAuthorityError(
                    "projection attempt settlement belongs to a different claim owner"
                )
            if attempt.attempt_kind == "dispatch" and (
                attempt.dispatch_claim_token != claim_token
                or attempt.dispatch_claim_revision != claim_revision
            ):
                raise TransitionAuthorityError(
                    "dispatch attempt settlement requires its original durable claim"
                )
        elif decided_by == "marco":
            if (
                event.state not in {"uncertain", "blocked"}
                or event.claim_owner is not None
                or event.claim_token is not None
            ):
                raise TransitionAuthorityError(
                    "Marco recovery requires an unclaimed uncertain or blocked event"
                )
        else:
            raise TransitionAuthorityError("unknown projection settlement authority")

        evidence_payload = dict(evidence)
        externally_observed = self._is_independent_external_observation(
            event=event,
            attempt=attempt,
            observation_kind=observation_kind,
            observed_applied=observed_applied,
            observed_identity=observed_identity,
            evidence=evidence_payload,
        )
        if event.event_type == "create_task":
            intended_identity = event.idempotency_key
        elif event.event_type == "reproject":
            intended_identity = self._reproject_state_identity(event) or attempt.request_sha256
        else:
            intended_identity = attempt.request_sha256
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
            evidence=evidence_payload,
            evidence_sha256=sha256_json(evidence_payload),
            observed_at=observed_at,
        )
        self.session.add(observation)
        self.session.flush()
        decision = adjudicate_effect(
            intended_identity=intended_identity,
            observation=EffectObservation(
                intended_identity=intended_identity,
                observed_identity=observed_identity,
                observed_applied=observed_applied,
                reread_complete=reread_complete,
                externally_observed=externally_observed,
                evidence=evidence_payload,
            ),
        )
        outcome = decision.outcome
        if event.event_type != "create_task" and not externally_observed:
            decision_reason = (
                "non-create settlement lacks independent operation-specific external observation"
            )
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
        claim_token: uuid.UUID,
        claim_revision: int,
        worker_id: str,
    ) -> tx.ProjectionCreateCorrelation:
        epoch, event = self._lock_event_path(event_id)
        self._assert_worker_claim(
            event=event,
            claim_token=claim_token,
            claim_revision=claim_revision,
            worker_id=worker_id,
            at=observed_at,
        )
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
        attempt = self._lock_attempt(attempt_id)
        if (
            attempt is None
            or attempt.projection_event_id != event_id
            or attempt.state != "dispatched"
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
        else:
            external_id = matches[0]
            if not external_id.isdigit() or external_id.startswith("0"):
                raise TransitionAuthorityError("Asana correlation match must be a canonical GID")
            if epoch.status != "active":
                raise TransitionAuthorityError("active projection epoch is required")
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
        _epoch, event, prior = self._lock_attempt_path(attempt_id)
        if prior.state not in {"uncertain", "blocked"}:
            raise TransitionAuthorityError(
                "recover targets one exact terminal uncertain or blocked attempt"
            )
        if (
            event.state not in {"uncertain", "blocked"}
            or event.claim_owner is not None
            or event.claim_token is not None
        ):
            raise TransitionAuthorityError("recover target event is not available for Marco recovery")
        if expected_task_id is not None and event.task_id != expected_task_id:
            raise TransitionAuthorityError("projection attempt does not belong to the command task")
        observed_applied = arguments.get("observed_applied")
        if observed_applied not in {True, False, None}:
            raise TransitionAuthorityError("observed_applied must be true, false, or null")
        latest_number = int(
            self.session.scalar(
                select(func.coalesce(func.max(tx.ProjectionAttempt.attempt_number), 0)).where(
                    tx.ProjectionAttempt.projection_event_id == event.projection_event_id
                )
            )
            or 0
        )
        recovery = tx.ProjectionAttempt(
            attempt_id=self.uuid_factory(),
            projection_event_id=event.projection_event_id,
            attempt_number=latest_number + 1,
            attempt_kind="recovery",
            predecessor_attempt_id=prior.attempt_id,
            worker_id=actor,
            request_identity=prior.request_identity,
            dispatch_identity=sha256_json(
                {
                    "projection_event_id": str(event.projection_event_id),
                    "predecessor_attempt_id": str(prior.attempt_id),
                    "attempt_number": latest_number + 1,
                    "attempt_kind": "marco_recovery",
                }
            ),
            retry_generation=prior.retry_generation,
            dispatch_claim_token=None,
            dispatch_claim_revision=None,
            intended_external_id=prior.intended_external_id,
            request_payload=dict(prior.request_payload),
            request_sha256=prior.request_sha256,
            state="dispatched",
            started_at=recovered_at,
            terminal_at=None,
        )
        self.session.add(recovery)
        self.session.flush()
        adjudication = self.record_observation_and_adjudicate(
            attempt_id=recovery.attempt_id,
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
            "attempt_id": str(recovery.attempt_id),
            "predecessor_attempt_id": str(prior.attempt_id),
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
            origin="live",
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

    def _reconciliation_run_by_key(
        self,
        *,
        generation_id: uuid.UUID,
        projection_epoch_id: uuid.UUID,
        corpus_identity: str,
        for_update: bool,
    ) -> tx.ProjectionReconciliationRun | None:
        statement = select(tx.ProjectionReconciliationRun).where(
            tx.ProjectionReconciliationRun.generation_id == generation_id,
            tx.ProjectionReconciliationRun.projection_epoch_id == projection_epoch_id,
            tx.ProjectionReconciliationRun.corpus_identity == corpus_identity,
        )
        if for_update and self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _lock_reconciliation_run(
        self, reconciliation_run_id: uuid.UUID
    ) -> tx.ProjectionReconciliationRun | None:
        statement = select(tx.ProjectionReconciliationRun).where(
            tx.ProjectionReconciliationRun.reconciliation_run_id == reconciliation_run_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def _assert_reconciliation_start_compatible(
        self,
        run: tx.ProjectionReconciliationRun,
        *,
        expected_items: int,
    ) -> None:
        if run.expected_items != expected_items:
            raise TransitionAuthorityError("reconciliation corpus immutable inputs conflict")
        if run.candidate_id is not None:
            raise TransitionAuthorityError("reconciliation run authority conflict")
        recorded_items = int(
            self.session.scalar(
                select(func.count())
                .select_from(tx.ProjectionReconciliationItem)
                .where(
                    tx.ProjectionReconciliationItem.reconciliation_run_id
                    == run.reconciliation_run_id
                )
            )
            or 0
        )
        if recorded_items != run.processed_items:
            raise TransitionAuthorityError("reconciliation run progress is inconsistent")

    def start_reconciliation(
        self,
        *,
        generation_id: uuid.UUID,
        corpus_identity: str,
        expected_items: int,
        started_at: datetime,
    ) -> tx.ProjectionReconciliationRun:
        if not corpus_identity.strip():
            raise TransitionAuthorityError("reconciliation corpus identity is required")
        if expected_items < 0:
            raise TransitionAuthorityError("reconciliation expected item count cannot be negative")
        epoch = self._active_epoch_for_generation(generation_id, shared=True)
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if epoch is None or generation is None or generation.status != "active":
            raise TransitionAuthorityError("active projection epoch is required")
        existing = self._reconciliation_run_by_key(
            generation_id=generation_id,
            projection_epoch_id=epoch.projection_epoch_id,
            corpus_identity=corpus_identity,
            for_update=True,
        )
        if existing is not None:
            self._assert_reconciliation_start_compatible(
                existing, expected_items=expected_items
            )
            return existing

        reconciliation_run_id = self.uuid_factory()
        row_values = {
            "reconciliation_run_id": reconciliation_run_id,
            "generation_id": generation_id,
            "projection_epoch_id": epoch.projection_epoch_id,
            "corpus_identity": corpus_identity,
            "status": "running",
            "expected_items": expected_items,
            "processed_items": 0,
            "started_at": started_at,
            "completed_at": None,
        }
        if self.session.get_bind().dialect.name != "postgresql":
            row = tx.ProjectionReconciliationRun(**row_values)
            self.session.add(row)
            self.session.flush()
            return row

        inserted_run_id = self.session.scalar(
            postgresql_insert(tx.ProjectionReconciliationRun)
            .values(**row_values)
            .on_conflict_do_nothing(constraint="uq_reconciliation_corpus")
            .returning(tx.ProjectionReconciliationRun.reconciliation_run_id)
        )
        if inserted_run_id is not None:
            inserted = self._lock_reconciliation_run(inserted_run_id)
            if inserted is None:
                raise TransitionAuthorityError("reconciliation insert was not observable")
            return inserted

        existing = self._reconciliation_run_by_key(
            generation_id=generation_id,
            projection_epoch_id=epoch.projection_epoch_id,
            corpus_identity=corpus_identity,
            for_update=True,
        )
        if existing is None:
            raise TransitionAuthorityError("reconciliation conflict did not resolve")
        self._assert_reconciliation_start_compatible(
            existing, expected_items=expected_items
        )
        return existing

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
        run = self._lock_reconciliation_run(reconciliation_run_id)
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
        run = self._lock_reconciliation_run(reconciliation_run_id)
        if run is None or run.status != "running":
            raise TransitionAuthorityError("reconciliation run is not active")
        if run.candidate_id is not None:
            raise TransitionAuthorityError(
                "candidate-bound reconciliation completion requires release authority"
            )
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
