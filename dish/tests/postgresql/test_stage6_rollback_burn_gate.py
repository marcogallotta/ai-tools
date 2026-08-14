from __future__ import annotations
from datetime import timedelta
import json
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from dish_pg.release import ALEMBIC_HEAD, ReleaseAuthorityError, ReleaseCandidateService
from dish_pg.release_status import AcceptanceCheck, CandidateEvaluation
from dish_pg.workflow import (
    ExecutionSpec,
    MutationAdmissionClosed,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
    sha256_json,
)
from tests.support.postgresql.first_admission import (
    _prepare_approved_cutover,
    _activate_authority,
    _assert_admission_closed,
    _burn_and_open_admission,
    _record_committed_first_request,
    _verify_and_complete,
    open_verified_first_admission,
)
from tests.support.postgresql.release import (
    HASH_A,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _record_runtime_and_worker_readiness_report,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db



def test_rollback_burn_rechecks_candidate_quiescence_immediately_before_burn(
    workflow_db, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)

    calls: list[tuple[object, object]] = []
    with session_scope(factory) as session:
        ordering: list[str] = []
        service = ReleaseCandidateService(
            session,
            uuid_factory=lambda: _next(ids),
            rollback_burn_fence_hook=lambda: ordering.append("fence"),
        )

        def failed_evaluation(*, candidate_id, as_of):
            ordering.append("evaluate")
            calls.append((candidate_id, as_of))
            return CandidateEvaluation(
                candidate_id=candidate_id,
                checks=(
                    AcceptanceCheck(
                        "quiescent_cutover_authority",
                        False,
                        {"authority_operations": 1},
                    ),
                ),
            )

        monkeypatch.setattr(service, "evaluate_candidate", failed_evaluation)
        with pytest.raises(
            ReleaseAuthorityError,
            match="failed immediately before rollback burn: quiescent_cutover_authority",
        ):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
                burned_at=NOW + timedelta(minutes=6),
            )
        assert ordering == ["fence", "evaluate"]
        assert calls == [(candidate_id, NOW + timedelta(minutes=6))]
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"],
                models.AuthorityActivation.outcome == "activated",
            )
        )
        assert activation is None


def test_rollback_burn_irreversibly_fences_external_projection(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)

    with session_scope(factory) as session:
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        assert candidate is not None
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        assert epoch is not None and epoch.external_effects_enabled is True

        projection.set_external_effects_enabled(
            projection_epoch_id=epoch.projection_epoch_id,
            enabled=False,
            reason="pre-burn operator pause",
        )
        projection.set_external_effects_enabled(
            projection_epoch_id=epoch.projection_epoch_id,
            enabled=True,
            reason="pre-burn operator resume",
        )
        assert epoch.external_effects_enabled is True

    burned_at = NOW + timedelta(minutes=6)
    with session_scope(factory) as session:
        release = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        release.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle:" + HASH_A,
            burned_at=burned_at,
            required_writer_inventory={"legacy-service@laptop"},
        )

        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        assert candidate is not None
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
        assert epoch is not None and epoch.external_effects_enabled is False

        with pytest.raises(TransitionAuthorityError, match="after rollback burn"):
            projection.set_external_effects_enabled(
                projection_epoch_id=epoch.projection_epoch_id,
                enabled=True,
                reason="attempt to resurrect projection",
            )
        with pytest.raises(TransitionAuthorityError, match="after rollback burn"):
            projection.activate_epoch(
                generation_id=candidate.generation_id,
                activation_reason="attempt to replace burned projection authority",
                created_at=burned_at + timedelta(seconds=1),
                external_effects_enabled=True,
            )
        assert epoch.external_effects_enabled is False

        intent = {"drift_kind": "historical", "authoritative_snapshot": {"task": "frozen"}}
        event = tx.ProjectionOutboxEvent(
            projection_event_id=_next(ids),
            generation_id=context["generation_id"],
            projection_epoch_id=epoch.projection_epoch_id,
            source_route="service",
            origin="live",
            command_execution_id=None,
            task_id=task_id,
            event_type="reproject",
            aggregate_sequence=1,
            idempotency_key="e" * 64,
            intent_payload=intent,
            intent_sha256=sha256_json(intent),
            state="pending",
            claim_owner=None,
            claim_token=None,
            claim_expires_at=None,
            outbox_revision=1,
            created_at=NOW,
            terminal_at=None,
        )
        session.add(event)
        session.flush()

        # Defense in depth: even a stale/corrupt true flag cannot make a burned
        # generation dispatchable through the repository worker claim path.
        epoch.external_effects_enabled = True
        session.flush()
        assert projection.claim_next(
            worker_id="projector", now=burned_at + timedelta(seconds=1), ttl=timedelta(minutes=2)
        ) is None
        assert event.state == "pending"
        epoch.external_effects_enabled = False
