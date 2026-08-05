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

from tests.support.postgresql.stage7_final_asana_closure import (
    _validate_and_approve,
)
from tests.support.postgresql.stage7_final_asana_closure import _case_test_final_asana_change_invalidates_activation_until_recaptured_and_recertified


def test_final_asana_change_invalidates_activation_until_recaptured_and_recertified(workflow_db) -> None:
    return _case_test_final_asana_change_invalidates_activation_until_recaptured_and_recertified(workflow_db)

def test_activation_requires_post_fence_closure_and_recertification(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        approved_closure = _record_final_closure(
            service, ids, candidate_id, closed_through_at=NOW + timedelta(minutes=2)
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve initial exact final Asana closure.",
            approval_payload={
                "final_asana_closure_id": str(approved_closure.closure_id),
                "final_asana_closure_sha256": approved_closure.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=3),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW + timedelta(minutes=4),
        )
        run = service.prepare_cutover(
            candidate_id=candidate_id, started_at=NOW + timedelta(minutes=4)
        )
        _record_and_engage_writer_fence(
            service, ids, fence_id=fence.fence_id, engaged_at=NOW + timedelta(minutes=5)
        )
        service.verify_writer_fence(
            fence_id=fence.fence_id,
            proof=_writer_fence_proof(fence, candidate_id),
            verified_at=NOW + timedelta(minutes=6),
            required_writer_inventory={fence.target_identity},
        )
        service.mark_fenced(
            cutover_run_id=run.cutover_run_id,
            recorded_at=NOW + timedelta(minutes=7),
            required_writer_inventory={fence.target_identity},
        )

        # Recertifying stale pre-fence evidence does not extend its observation interval.
        service.recertify_candidate(
            candidate_id=candidate_id,
            closure_id=approved_closure.closure_id,
            approver="Marco",
            recertification_statement="Confirm the selected closure after fencing.",
            payload={"cutover_run_id": str(run.cutover_run_id)},
            recertified_at=NOW + timedelta(minutes=8),
        )
        with pytest.raises(ReleaseAuthorityError, match="writer-fence boundary"):
            service.activate_authority(
                cutover_run_id=run.cutover_run_id,
                final_asana_closure_id=approved_closure.closure_id,
                activated_at=NOW + timedelta(minutes=9),
                required_writer_inventory={fence.target_identity},
            )

        final = service.record_final_asana_closure(
            candidate_id=candidate_id,
            capture_manifest_sha256=HASH_A,
            observation_high_water="asana-change-post-fence-missing-recertification",
            watcher_identity="watcher@fixture",
            interval_started_at=NOW + timedelta(minutes=2),
            closed_through_at=NOW + timedelta(minutes=9),
            payload={"registry": "closed", "writer_fence_verified": True},
            recorded_at=NOW + timedelta(minutes=9),
        )
        with pytest.raises(ReleaseAuthorityError, match="currently approved"):
            service.activate_authority(
                cutover_run_id=run.cutover_run_id,
                final_asana_closure_id=final.closure_id,
                activated_at=NOW + timedelta(minutes=10),
                required_writer_inventory={fence.target_identity},
            )

def test_valid_increasing_final_asana_activation_chronology_passes(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        initial = _record_final_closure(
            service, ids, candidate_id, closed_through_at=NOW + timedelta(minutes=2)
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve initial closure evidence.",
            approval_payload={
                "final_asana_closure_id": str(initial.closure_id),
                "final_asana_closure_sha256": initial.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=3),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW + timedelta(minutes=4),
        )
        run = service.prepare_cutover(
            candidate_id=candidate_id, started_at=NOW + timedelta(minutes=4)
        )
        _record_and_engage_writer_fence(
            service, ids, fence_id=fence.fence_id, engaged_at=NOW + timedelta(minutes=5)
        )
        service.verify_writer_fence(
            fence_id=fence.fence_id,
            proof=_writer_fence_proof(fence, candidate_id),
            verified_at=NOW + timedelta(minutes=6),
            required_writer_inventory={fence.target_identity},
        )
        service.mark_fenced(
            cutover_run_id=run.cutover_run_id,
            recorded_at=NOW + timedelta(minutes=7),
            required_writer_inventory={fence.target_identity},
        )
        final = service.record_final_asana_closure(
            candidate_id=candidate_id,
            capture_manifest_sha256=HASH_A,
            observation_high_water="asana-change-post-fence",
            watcher_identity="watcher@fixture",
            interval_started_at=NOW + timedelta(minutes=2),
            closed_through_at=NOW + timedelta(minutes=8),
            payload={"registry": "closed", "writer_fence_verified": True},
            recorded_at=NOW + timedelta(minutes=8),
        )
        service.recertify_candidate(
            candidate_id=candidate_id,
            closure_id=final.closure_id,
            approver="Marco",
            recertification_statement="Authorize the post-fence final verification.",
            payload={"cutover_run_id": str(run.cutover_run_id)},
            recertified_at=NOW + timedelta(minutes=9),
        )
        with pytest.raises(ReleaseAuthorityError, match="final Asana recertification"):
            service.activate_authority(
                cutover_run_id=run.cutover_run_id,
                final_asana_closure_id=final.closure_id,
                activated_at=NOW + timedelta(minutes=8, seconds=30),
                required_writer_inventory={fence.target_identity},
            )
        activated = service.activate_authority(
            cutover_run_id=run.cutover_run_id,
            final_asana_closure_id=final.closure_id,
            activated_at=NOW + timedelta(minutes=10),
            required_writer_inventory={fence.target_identity},
        )
        assert activated.state == "activated"

def test_final_asana_closure_rejects_impossible_or_future_chronology(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW,
        )
        with pytest.raises(ReleaseAuthorityError, match="closed_through_at"):
            service.record_final_asana_closure(
                candidate_id=candidate_id,
                capture_manifest_sha256=HASH_A,
                observation_high_water="asana-change-1",
                watcher_identity="watcher@fixture",
                interval_started_at=NOW + timedelta(minutes=2),
                closed_through_at=NOW + timedelta(minutes=1),
                payload={"registry": "closed"},
                recorded_at=NOW + timedelta(minutes=2),
            )
        trusted = ReleaseCandidateService(
            session,
            uuid_factory=lambda: _next(ids),
            clock=lambda: NOW + timedelta(minutes=2),
        )
        with pytest.raises(ReleaseAuthorityError, match="trusted database clock"):
            trusted.record_final_asana_closure(
                candidate_id=candidate_id,
                capture_manifest_sha256=HASH_A,
                observation_high_water="asana-change-2",
                watcher_identity="watcher@fixture",
                interval_started_at=NOW,
                closed_through_at=NOW + timedelta(minutes=2),
                payload={"registry": "closed"},
                recorded_at=NOW + timedelta(minutes=3),
            )

def test_stage7_migration_adds_immutable_final_closure_authority(tmp_path) -> None:
    config = Config(str((__import__("pathlib").Path(__file__).resolve().parents[2]) / "alembic.ini"))
    path = tmp_path / "stage7.sqlite3"
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "head")
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        names = set(inspect(engine).get_table_names())
        assert {
            "final_asana_closures",
            "final_asana_closure_invalidations",
            "cutover_recertifications",
        }.issubset(names)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()

def test_stage7_operator_cli_exposes_closure_and_recertification_commands() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-release"))
    parser = namespace["_parser"]()
    assert parser.parse_args([
        "final-asana-closure-record",
        "00000000-0000-0000-0000-000000000001",
        "--file",
        "/tmp/closure.json",
    ]).command == "final-asana-closure-record"
    assert parser.parse_args([
        "candidate-recertify",
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "--file",
        "/tmp/recertify.json",
    ]).command == "candidate-recertify"
    activation = parser.parse_args([
        "cutover-activate",
        "00000000-0000-0000-0000-000000000003",
        "--final-closure-id",
        "00000000-0000-0000-0000-000000000002",
        "--activated-at",
        "2026-08-02T10:00:00+02:00",
    ])
    assert activation.final_closure_id == "00000000-0000-0000-0000-000000000002"
