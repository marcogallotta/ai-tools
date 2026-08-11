"""Final Asana-authority closure and candidate recertification transitions."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import func, select

from . import stage6_models as rel
from .candidate_manifest import bind_approval_manifest, build_candidate_manifest
from .cutover_chronology import _require_at_or_after, _require_aware
from .release_history import (
    acquire_active_registry_release_gate,
    acquire_generation_release_gate,
)
from .release_evidence import (
    ReleaseAuthorityError,
    _require_nonblank,
    _require_sha256,
    sha256_json,
)


class FinalAsanaClosureAuthority:
    def record_final_asana_closure(
        self,
        *,
        candidate_id: uuid.UUID,
        capture_manifest_sha256: str,
        observation_high_water: str,
        watcher_identity: str,
        interval_started_at: datetime,
        closed_through_at: datetime,
        payload: Mapping[str, Any],
        recorded_at: datetime,
    ) -> rel.FinalAsanaClosure:
        candidate = self._candidate(candidate_id)
        _require_aware(interval_started_at, "interval_started_at")
        _require_aware(closed_through_at, "closed_through_at")
        _require_aware(recorded_at, "recorded_at")
        if candidate.status not in {"validated", "approved"}:
            raise ReleaseAuthorityError("final Asana closure requires a validated candidate")
        capture_manifest_sha256 = _require_sha256(
            capture_manifest_sha256, "capture_manifest_sha256"
        )
        _require_nonblank(observation_high_water, "observation_high_water")
        _require_nonblank(watcher_identity, "watcher_identity")
        _require_at_or_after(
            closed_through_at,
            interval_started_at,
            field="closed_through_at",
            floor_field="interval_started_at",
        )
        _require_at_or_after(
            recorded_at,
            closed_through_at,
            field="recorded_at",
            floor_field="closed_through_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        body = {
            "candidate_id": str(candidate_id),
            "capture_manifest_sha256": capture_manifest_sha256,
            "observation_high_water": observation_high_water,
            "watcher_identity": watcher_identity,
            "interval_started_at": interval_started_at.isoformat(),
            "closed_through_at": closed_through_at.isoformat(),
            "payload": dict(payload),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.FinalAsanaClosure).where(
                rel.FinalAsanaClosure.candidate_id == candidate_id,
                rel.FinalAsanaClosure.closure_sha256 == digest,
            )
        )
        if existing is not None:
            return existing
        row = rel.FinalAsanaClosure(
            closure_id=self.uuid_factory(),
            candidate_id=candidate_id,
            capture_manifest_sha256=capture_manifest_sha256,
            observation_high_water=observation_high_water.strip(),
            watcher_identity=watcher_identity.strip(),
            interval_started_at=interval_started_at,
            closed_through_at=closed_through_at,
            payload=dict(payload),
            closure_sha256=digest,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row
    def invalidate_final_asana_closure(
        self,
        *,
        closure_id: uuid.UUID,
        change_identity: str,
        change_kind: str,
        payload: Mapping[str, Any],
        observed_at: datetime,
        recorded_at: datetime,
    ) -> rel.FinalAsanaClosureInvalidation:
        _require_aware(observed_at, "observed_at")
        _require_aware(recorded_at, "recorded_at")
        closure = self.session.get(rel.FinalAsanaClosure, closure_id)
        if closure is None:
            raise ReleaseAuthorityError("unknown final Asana closure")
        _require_nonblank(change_identity, "change_identity")
        _require_nonblank(change_kind, "change_kind")
        _require_at_or_after(
            observed_at,
            closure.interval_started_at,
            field="observed_at",
            floor_field="closure.interval_started_at",
        )
        _require_at_or_after(
            recorded_at,
            observed_at,
            field="recorded_at",
            floor_field="observed_at",
        )
        _require_at_or_after(
            recorded_at,
            closure.recorded_at,
            field="recorded_at",
            floor_field="closure.recorded_at",
        )
        self._require_not_future(recorded_at, "recorded_at")
        body = {
            "closure_id": str(closure_id),
            "change_identity": change_identity,
            "change_kind": change_kind,
            "payload": dict(payload),
            "observed_at": observed_at.isoformat(),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.FinalAsanaClosureInvalidation).where(
                rel.FinalAsanaClosureInvalidation.closure_id == closure_id
            )
        )
        if existing is not None:
            if existing.invalidation_sha256 != digest:
                raise ReleaseAuthorityError("final Asana closure invalidation conflict")
            return existing
        row = rel.FinalAsanaClosureInvalidation(
            invalidation_id=self.uuid_factory(),
            closure_id=closure_id,
            change_identity=change_identity.strip(),
            change_kind=change_kind.strip(),
            payload=dict(payload),
            invalidation_sha256=digest,
            observed_at=observed_at,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row
    def recertify_candidate(
        self,
        *,
        candidate_id: uuid.UUID,
        closure_id: uuid.UUID,
        approver: str,
        recertification_statement: str,
        payload: Mapping[str, Any],
        recertified_at: datetime,
    ) -> rel.CutoverRecertification:
        candidate = self._candidate(candidate_id)
        if candidate.status != "approved":
            raise ReleaseAuthorityError("recertification requires an approved candidate")
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        closure = self._valid_final_asana_closure(candidate_id, closure_id)
        if approval is None:
            raise ReleaseAuthorityError("candidate has no approval to recertify")
        _require_at_or_after(
            recertified_at,
            closure.recorded_at,
            field="recertified_at",
            floor_field="final closure recorded_at",
        )
        _require_at_or_after(
            recertified_at,
            approval.approved_at,
            field="recertified_at",
            floor_field="approved_at",
        )
        self._require_not_future(recertified_at, "recertified_at")
        revision = int(
            self.session.scalar(
                select(func.coalesce(func.max(rel.CutoverRecertification.recertification_revision), 0)).where(
                    rel.CutoverRecertification.candidate_id == candidate_id
                )
            ) or 0
        ) + 1
        body = {
            "candidate_id": str(candidate_id),
            "approval_id": str(approval.approval_id),
            "closure_id": str(closure.closure_id),
            "closure_sha256": closure.closure_sha256,
            "revision": revision,
            "approver": approver,
            "statement": recertification_statement,
            "payload": dict(payload),
            "recertified_at": recertified_at.isoformat(),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.CutoverRecertification).where(
                rel.CutoverRecertification.closure_id == closure_id
            )
        )
        if existing is not None:
            if existing.recertification_sha256 != digest:
                raise ReleaseAuthorityError("cutover recertification identity conflict")
            return existing
        row = rel.CutoverRecertification(
            recertification_id=self.uuid_factory(),
            candidate_id=candidate_id,
            approval_id=approval.approval_id,
            closure_id=closure.closure_id,
            recertification_revision=revision,
            approver=approver.strip(),
            recertification_statement=recertification_statement.strip(),
            payload=dict(payload),
            recertification_sha256=digest,
            recertified_at=recertified_at,
        )
        self.session.add(row)
        self.session.flush()
        return row
    def approve_candidate(
        self,
        *,
        candidate_id: uuid.UUID,
        evidence_bundle_id: uuid.UUID,
        approver: str,
        approval_statement: str,
        approval_payload: Mapping[str, Any],
        approved_at: datetime,
    ) -> rel.CutoverApproval:
        candidate = self._candidate(candidate_id)
        generation = acquire_generation_release_gate(
            self.session, generation_id=candidate.generation_id
        )
        if generation is None or generation.status != "active":
            raise ReleaseAuthorityError(
                "candidate approval requires the active target generation"
            )
        active_registry = acquire_active_registry_release_gate(
            self.session, generation_id=candidate.generation_id
        )
        if active_registry is None:
            raise ReleaseAuthorityError(
                "candidate approval requires the active section registry"
            )
        candidate = self.session.scalar(
            select(rel.ReleaseCandidate)
            .where(rel.ReleaseCandidate.candidate_id == candidate_id)
            .execution_options(populate_existing=True)
        )
        if candidate is None:
            raise ReleaseAuthorityError("unknown release candidate")
        if (
            active_registry.generation_id != candidate.generation_id
            or active_registry.registry_version_id != candidate.registry_version_id
        ):
            raise ReleaseAuthorityError(
                "release candidate identity does not match active generation → registry → Honest binding"
            )
        self._require_candidate_release_identity(candidate)
        bundle = self.session.get(rel.EvidenceBundle, evidence_bundle_id)
        if candidate.status not in {"validated", "approved"}:
            raise ReleaseAuthorityError("candidate must be validated before approval")
        if (
            bundle is None
            or bundle.candidate_id != candidate_id
            or bundle.manifest_sha256 != candidate.validation_bundle_sha256
        ):
            raise ReleaseAuthorityError("approval must bind the exact validated evidence bundle")
        closure_id_value = approval_payload.get("final_asana_closure_id")
        closure_sha_value = approval_payload.get("final_asana_closure_sha256")
        if closure_id_value is None or closure_sha_value is None:
            raise ReleaseAuthorityError("approval must bind the exact final Asana closure")
        closure_sha_value = _require_sha256(
            closure_sha_value, "final_asana_closure_sha256"
        )
        try:
            closure_id = uuid.UUID(str(closure_id_value))
        except ValueError as exc:
            raise ReleaseAuthorityError("approval final Asana closure identity is invalid") from exc
        closure = self._valid_final_asana_closure(candidate_id, closure_id)
        if closure.closure_sha256 != str(closure_sha_value):
            raise ReleaseAuthorityError("approval final Asana closure digest mismatch")
        if candidate.validated_at is None:
            raise ReleaseAuthorityError("validated candidate lacks validation chronology")
        _require_at_or_after(
            approved_at,
            candidate.validated_at,
            field="approved_at",
            floor_field="validated_at",
        )
        _require_at_or_after(
            approved_at,
            closure.recorded_at,
            field="approved_at",
            floor_field="final closure recorded_at",
        )
        self._require_not_future(approved_at, "approved_at")
        evaluation = self.evaluate_candidate(
            candidate_id=candidate_id, as_of=approved_at
        )
        if not evaluation.passed:
            raise ReleaseAuthorityError(
                "candidate release gates are no longer satisfied at approval"
            )
        manifest = build_candidate_manifest(
            self.session,
            uuid_factory=self.uuid_factory,
            candidate=candidate,
            built_at=approved_at,
        )
        body = {
            "candidate_id": str(candidate_id),
            "evidence_bundle_sha256": bundle.manifest_sha256,
            "approver": approver,
            "statement": approval_statement,
            "payload": dict(approval_payload),
            "approved_at": approved_at.isoformat(),
        }
        digest = sha256_json(body)
        existing = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        if existing is not None:
            if existing.approval_sha256 != digest:
                raise ReleaseAuthorityError("cutover approval identity conflict")
            return existing
        row = rel.CutoverApproval(
            approval_id=self.uuid_factory(),
            candidate_id=candidate_id,
            evidence_bundle_id=evidence_bundle_id,
            approver=approver.strip(),
            approval_statement=approval_statement.strip(),
            approval_payload=dict(approval_payload),
            approval_sha256=digest,
            approved_at=approved_at,
        )
        self.session.add(row)
        self.session.flush()
        bind_approval_manifest(
            self.session, uuid_factory=self.uuid_factory, approval=row,
            candidate=candidate, bound_at=approved_at,
        )
        candidate.status = "approved"
        candidate.candidate_revision += 1
        candidate.approved_at = approved_at
        self.session.flush()
        return row
    def _closure_invalidation(
        self, closure_id: uuid.UUID
    ) -> rel.FinalAsanaClosureInvalidation | None:
        return self.session.scalar(
            select(rel.FinalAsanaClosureInvalidation).where(
                rel.FinalAsanaClosureInvalidation.closure_id == closure_id
            )
        )
    def _valid_final_asana_closure(
        self, candidate_id: uuid.UUID, closure_id: uuid.UUID
    ) -> rel.FinalAsanaClosure:
        closure = self.session.get(rel.FinalAsanaClosure, closure_id)
        if closure is None or closure.candidate_id != candidate_id:
            raise ReleaseAuthorityError("final Asana closure does not belong to candidate")
        if self._closure_invalidation(closure_id) is not None:
            raise ReleaseAuthorityError("final Asana closure was invalidated by an intervening change")
        return closure
    def _current_approved_final_asana_closure(
        self,
        candidate_id: uuid.UUID,
        *,
        expected_closure_id: uuid.UUID | None = None,
    ) -> rel.FinalAsanaClosure:
        approval = self.session.scalar(
            select(rel.CutoverApproval).where(rel.CutoverApproval.candidate_id == candidate_id)
        )
        if approval is None:
            raise ReleaseAuthorityError("candidate has no cutover approval")
        recertification = self.session.scalar(
            select(rel.CutoverRecertification)
            .where(rel.CutoverRecertification.candidate_id == candidate_id)
            .order_by(rel.CutoverRecertification.recertification_revision.desc())
            .limit(1)
        )
        if recertification is not None:
            closure_id = recertification.closure_id
        else:
            value = approval.approval_payload.get("final_asana_closure_id")
            if value is None:
                raise ReleaseAuthorityError("cutover approval lacks final Asana closure binding")
            closure_id = uuid.UUID(str(value))
        if expected_closure_id is not None and closure_id != expected_closure_id:
            raise ReleaseAuthorityError("cutover does not bind the currently approved final Asana closure")
        closure = self._valid_final_asana_closure(candidate_id, closure_id)
        if recertification is None:
            expected_sha = approval.approval_payload.get("final_asana_closure_sha256")
            if expected_sha != closure.closure_sha256:
                raise ReleaseAuthorityError("cutover approval final Asana closure digest mismatch")
        return closure
