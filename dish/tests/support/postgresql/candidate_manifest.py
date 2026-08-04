"""Helpers for candidate authority-manifest regression coverage."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import stage5_models as tx
from dish_pg.candidate_manifest import revalidate_candidate_manifest
from dish_pg.database import session_scope
from tests.support.postgresql.release import HASH_A, _prepare_candidate, _record_final_closure
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _validated_candidate(session, ids, context, task_id):
    service, candidate_id = _prepare_candidate(session, ids, context, task_id)
    bundle = service.build_evidence_bundle(
        candidate_id=candidate_id,
        bundle_kind="release_candidate",
        built_at=NOW,
    )
    service.validate_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        validated_at=NOW + timedelta(minutes=1),
    )
    closure = _record_final_closure(
        service,
        ids,
        candidate_id,
        closed_through_at=NOW + timedelta(minutes=2),
    )
    return service, candidate_id, bundle, closure


def _approve(session, service, candidate_id, bundle, closure):
    service.approve_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="Marco",
        approval_statement="Approve exact deep authority manifest.",
        approval_payload={
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=closure.recorded_at,
    )
    manifest = session.scalar(
        select(manifest_models.ReleaseCandidateManifest).where(
            manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id
        )
    )
    assert manifest is not None
    return manifest


def _revalidate(session, ids, service, candidate_id):
    return revalidate_candidate_manifest(
        session,
        uuid_factory=lambda: _next(ids),
        candidate=service._candidate(candidate_id),
        revalidated_at=NOW + timedelta(minutes=4),
    )

