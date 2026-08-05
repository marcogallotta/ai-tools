from __future__ import annotations
from datetime import timedelta
import runpy
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from dish_pg import models
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD, ReleaseAuthorityError, ReleaseCandidateService
from tests.support.postgresql.workflow import NOW, _next, workflow_db
from tests.support.postgresql.release import (
    HASH_A,
    ROOT,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _writer_fence_proof,
)

def _validate_and_approve(service, ids, candidate_id, closure):
    bundle = service.build_evidence_bundle(
        candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
    )
    service.validate_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        validated_at=NOW + timedelta(minutes=1),
    )
    service.approve_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="Marco",
        approval_statement="Approve the exact candidate through the final observed Asana closure.",
        approval_payload={
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=closure.recorded_at,
    )


def _case_test_final_asana_change_invalidates_activation_until_recaptured_and_recertified(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        # Closure is a post-validation authority fact, so validate first.
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        original = _record_final_closure(
            service, ids, candidate_id, closed_through_at=NOW + timedelta(minutes=8)
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve the exact captured Asana authority state.",
            approval_payload={
                "final_asana_closure_id": str(original.closure_id),
                "final_asana_closure_sha256": original.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=8),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW + timedelta(minutes=8),
        )
        cutover = service.prepare_cutover(
            candidate_id=candidate_id, started_at=NOW + timedelta(minutes=8)
        )
        _record_and_engage_writer_fence(
            service, ids, fence_id=fence.fence_id, engaged_at=NOW + timedelta(minutes=8)
        )
        service.verify_writer_fence(
            fence_id=fence.fence_id,
            proof=_writer_fence_proof(fence, candidate_id),
            verified_at=NOW + timedelta(minutes=8),
            required_writer_inventory={fence.target_identity},
        )
        service.mark_fenced(
            cutover_run_id=cutover.cutover_run_id,
            recorded_at=NOW + timedelta(minutes=8),
            required_writer_inventory={fence.target_identity},
        )
        service.invalidate_final_asana_closure(
            closure_id=original.closure_id,
            change_identity="asana-event-901",
            change_kind="task_content_changed",
            payload={"task_gid": "123456789"},
            observed_at=NOW + timedelta(minutes=5),
            recorded_at=NOW + timedelta(minutes=8),
        )
        with pytest.raises(ReleaseAuthorityError, match="invalidated"):
            service.activate_authority(
                cutover_run_id=cutover.cutover_run_id,
                final_asana_closure_id=original.closure_id,
                activated_at=NOW + timedelta(minutes=8),
                required_writer_inventory={fence.target_identity},
            )

        replacement = service.record_final_asana_closure(
            candidate_id=candidate_id,
            capture_manifest_sha256="b" * 64,
            observation_high_water="asana-event-901",
            watcher_identity="final-asana-watcher@production",
            interval_started_at=NOW + timedelta(minutes=5),
            closed_through_at=NOW + timedelta(minutes=9),
            payload={"recaptured": True, "tasks": 1, "registry": "closed"},
            recorded_at=NOW + timedelta(minutes=9),
        )
        service.recertify_candidate(
            candidate_id=candidate_id,
            closure_id=replacement.closure_id,
            approver="Marco",
            recertification_statement="Recertify after exact recapture of the intervening change.",
            payload={"change_identity": "asana-event-901"},
            recertified_at=NOW + timedelta(minutes=9),
        )
        activated = service.activate_authority(
            cutover_run_id=cutover.cutover_run_id,
            final_asana_closure_id=replacement.closure_id,
            activated_at=NOW + timedelta(minutes=9),
            required_writer_inventory={fence.target_identity},
        )
        assert activated.state == "activated"
        service.burn_rollback(
            cutover_run_id=cutover.cutover_run_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=10),
            required_writer_inventory={fence.target_identity},
        )
        checkpoint = session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover.cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == "authority_activated_admission_closed",
            )
        )
        assert checkpoint.payload["final_asana_closure_id"] == str(replacement.closure_id)
