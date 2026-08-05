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
    _record_runtime_and_typed_readiness,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db



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
def test_first_admission_rejects_non_strict_post_request_reconciliation(
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
        with pytest.raises(
            ReleaseAuthorityError,
            match="execution, audit, projection, and reconciliation",
        ):
            service.verify_first_admission(
                cutover_run_id=cutover_id,
                request_id=request_id,
                verified_at=verified_at,
            )

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
        assert ALEMBIC_HEAD == "0029_cutover_authority_admission_fixes"
    finally:
        engine.dispose()
