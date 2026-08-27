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
from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
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




def _cutover_rehearsal_id(session, candidate_id):
    rehearsal = session.scalar(
        select(rel.RehearsalRun).where(
            rel.RehearsalRun.candidate_id == candidate_id,
            rel.RehearsalRun.rehearsal_kind == "cutover",
        )
    )
    assert rehearsal is not None
    return rehearsal.rehearsal_id


def test_admission_open_rehearsal_teardown_is_durable_idempotent_and_unblocks_generation(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id, rehearsal_kind="cutover"
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _request_id, _run_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )

    torn_down_at = NOW + timedelta(minutes=8)
    reason = "abandoned TEST cutover rehearsal before first request"
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        rehearsal_id = _cutover_rehearsal_id(session, candidate_id)
        unrelated = session.scalar(
            select(rel.RehearsalRun).where(
                rel.RehearsalRun.candidate_id == candidate_id,
                rel.RehearsalRun.rehearsal_kind == "activation",
            )
        )
        assert unrelated is not None
        with pytest.raises(ReleaseAuthorityError, match="identity conflict"):
            service.teardown_rehearsal_cutover(
                cutover_run_id=cutover_id,
                rehearsal_id=unrelated.rehearsal_id,
                reason=reason,
                torn_down_at=torn_down_at,
            )
        with pytest.raises(ReleaseAuthorityError, match="ordinary rollback is prohibited"):
            service.abort_cutover(
                cutover_run_id=cutover_id,
                reason="must not weaken ordinary abort",
                aborted_at=torn_down_at,
            )
        torn_down = service.teardown_rehearsal_cutover(
            cutover_run_id=cutover_id,
            rehearsal_id=rehearsal_id,
            reason=reason,
            torn_down_at=torn_down_at,
        )
        assert torn_down.state == "rehearsal_torn_down"
        reservation = session.scalar(
            select(reservations.FirstRequestReservation).where(
                reservations.FirstRequestReservation.cutover_run_id == cutover_id
            )
        )
        assert reservation is not None and reservation.state == "cancelled"
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None and control.state == "closed" and control.opened_at is None
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.rehearsal_id == rehearsal_id
            )
        )
        assert activation is not None and activation.outcome == "aborted"
        assert activation.rollback_burned_at is None
        assert session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"],
                models.AuthorityActivation.outcome == "activated",
            )
        ) is None
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        rehearsal = session.get(rel.RehearsalRun, rehearsal_id)
        assert candidate is not None and candidate.status == "aborted"
        assert rehearsal is not None and rehearsal.status == "failed"

    # A new service/session must observe the same exact terminal proof and replay safely.
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        replay = service.teardown_rehearsal_cutover(
            cutover_run_id=cutover_id,
            rehearsal_id=rehearsal_id,
            reason=reason,
            torn_down_at=torn_down_at,
        )
        assert replay.state == "rehearsal_torn_down"
        checkpoint = session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_id,
                rel.CutoverCheckpoint.checkpoint_kind == "rehearsal_cutover_torn_down",
            )
        )
        assert checkpoint is not None
        assert checkpoint.payload["reservation_state"] == "cancelled"
        with pytest.raises(ReleaseAuthorityError, match="replay identity conflict"):
            service.teardown_rehearsal_cutover(
                cutover_run_id=cutover_id,
                rehearsal_id=rehearsal_id,
                reason="different reason",
                torn_down_at=torn_down_at,
            )


def test_rehearsal_teardown_rejects_equivalent_real_cutover(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _burn_and_open_admission(factory, ids, context, task_id, candidate_id, cutover_id)
    with session_scope(factory) as session:
        rehearsal = session.scalar(
            select(rel.RehearsalRun).where(rel.RehearsalRun.candidate_id == candidate_id)
        )
        assert rehearsal is not None
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        with pytest.raises(ReleaseAuthorityError, match="real cutover"):
            service.teardown_rehearsal_cutover(
                cutover_run_id=cutover_id,
                rehearsal_id=rehearsal.rehearsal_id,
                reason="must fail",
                torn_down_at=NOW + timedelta(minutes=8),
            )
        run = session.get(rel.CutoverRun, cutover_id)
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"],
                models.AuthorityActivation.outcome == "activated",
            )
        )
        assert run is not None and run.state == "admission_open"
        assert activation is not None and activation.rehearsal_id is None

def test_cutover_is_resumable_admission_stays_closed_until_burn_and_first_outcome(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _assert_admission_closed(factory, ids, context, task_id)
    request_id, run_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )
    _record_committed_first_request(
        factory, ids, context, task_id, cutover_id, request_id, run_id
    )
    _verify_and_complete(factory, ids, context, candidate_id, cutover_id, request_id)

@pytest.mark.parametrize("defect", ["stale", "wrong_candidate", "wrong_boundary"])
def test_first_admission_ignores_post_burn_external_reconciliation(
    workflow_db, defect: str
) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    request_id, run_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )
    _record_committed_first_request(
        factory, ids, context, task_id, cutover_id, request_id, run_id
    )

    verified_at = NOW + timedelta(minutes=9)
    with session_scope(factory) as session:
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity=f"post-first-admission-{defect}",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        if defect == "stale":
            verified_at = NOW + timedelta(hours=2)
        elif defect == "wrong_candidate":
            candidate = session.get(rel.ReleaseCandidate, candidate_id)
            assert candidate is not None
            other = rel.ReleaseCandidate(
                candidate_id=_next(ids),
                generation_id=candidate.generation_id,
                source_import_batch_id=candidate.source_import_batch_id,
                shadow_baseline_id=candidate.shadow_baseline_id,
                projection_epoch_id=candidate.projection_epoch_id,
                source_release=candidate.source_release,
                source_commit="f" * 64,
                ledger_through_commit=candidate.ledger_through_commit,
                schema_head=candidate.schema_head,
                dish_release=candidate.dish_release,
                honest_release=candidate.honest_release,
                protocol_release=candidate.protocol_release,
                openapi_release=candidate.openapi_release,
                routing_release=candidate.routing_release,
                status="assembling",
                candidate_revision=1,
                validation_bundle_sha256=None,
                created_at=candidate.created_at,
                validated_at=None,
                approved_at=None,
                terminal_at=None,
            )
            session.add(other)
            session.flush()
            other.status = "activated"
            other.candidate_revision = 2
            other.validation_bundle_sha256 = candidate.validation_bundle_sha256
            other.validated_at = candidate.validated_at
            other.approved_at = candidate.approved_at
            other.terminal_at = candidate.terminal_at
            session.flush()
            reconciliation.candidate_id = other.candidate_id
        else:
            reconciliation.external_high_water = None
            reconciliation.external_snapshot_identity = "snapshot:wrong-contract"
        session.flush()

        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        verified = service.verify_first_admission(
            cutover_run_id=cutover_id,
            request_id=request_id,
            verified_at=verified_at,
        )
        assert verified.state == "first_admission_verified"
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None and control.state == "open"

def test_rollback_burn_replay_requires_exact_bundle_and_timestamp(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)

    burned_at = NOW + timedelta(minutes=6)
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        activation = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle:" + HASH_A,
            burned_at=burned_at,
            required_writer_inventory={"legacy-service@laptop"},
        )
        replay = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle:" + HASH_A,
            burned_at=burned_at,
        )
        assert replay.activation_id == activation.activation_id

        with pytest.raises(ReleaseAuthorityError, match="identity conflict"):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle:" + "b" * 64,
                burned_at=burned_at,
            )
        with pytest.raises(ReleaseAuthorityError, match="identity conflict"):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle:" + HASH_A,
                burned_at=burned_at + timedelta(seconds=1),
            )
        with pytest.raises(ReleaseAuthorityError, match="nonblank"):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="   ",
                burned_at=burned_at,
            )

@pytest.mark.database_boundary
def test_rollback_bundle_identity_migration_adds_nonblank_constraint(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    path = tmp_path / "rollback-bundle.sqlite3"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "head")

    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        checks = {
            row["name"]: row["sqltext"]
            for row in inspect(engine).get_check_constraints("authority_activations")
        }
        assert "ck_authority_activations_legacy_bundle_nonblank" in checks
        assert "trim(legacy_bundle_id)" in checks[
            "ck_authority_activations_legacy_bundle_nonblank"
        ]
        assert "rehearsal_id" in {
            row["name"] for row in inspect(engine).get_columns("cutover_runs")
        }
        assert "rehearsal_id" in {
            row["name"] for row in inspect(engine).get_columns("authority_activations")
        }
        indexes = {
            row["name"]: row for row in inspect(engine).get_indexes("first_request_reservations")
        }
        assert indexes["uq_first_request_reservation_live_generation"]["unique"] == 1
        with engine.connect() as connection:
            index_sql = connection.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='uq_first_request_reservation_live_generation'"
                )
            )
        assert index_sql is not None
        assert "WHERE state IN ('reserved','consumed')" in index_sql
        assert ALEMBIC_HEAD == "0044_independent_archive"
    finally:
        engine.dispose()
