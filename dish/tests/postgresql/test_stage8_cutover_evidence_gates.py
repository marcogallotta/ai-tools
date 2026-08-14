from __future__ import annotations
import io
import json
import runpy
from datetime import timedelta, timezone
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import (
    ALEMBIC_HEAD,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    ReleaseAuthorityError,
    ReleaseCandidateService,
    sha256_json,
)
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    WorkflowAuthorityService,
)
from dish_service.legacy_writer_fence import (
    engage_legacy_writer_fence,
    observe_legacy_writer_fence,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import (
    DEFAULT_REHEARSAL_ENVIRONMENT,
    HASH_A,
    ROOT,
    _complete_active_mapping_reconciliation,
    _record_runtime_and_worker_readiness_report,
    _artifact_file,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _writer_fence_proof,
)

from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _burn_rollback,
    _record_runtime_and_worker_readiness,
    _prepare_fenced_recertified_cutover,
)


@pytest.mark.database_boundary
def test_stage8_schema_migration_adds_cutover_evidence_tables(tmp_path: Path) -> None:
    assert set(rel.STAGE8_TABLE_NAMES).issubset(models.Base.metadata.tables)

    offline = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    offline.attributes["output_buffer"] = buffer
    command.upgrade(offline, "head", sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE runtime_release_attestations" in rendered
    assert "CREATE TABLE projection_worker_readiness" in rendered
    assert "CREATE TABLE first_admission_plans" in rendered

    path = tmp_path / "stage8.sqlite3"
    online = Config(str(ROOT / "alembic.ini"))
    online.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(online, "head")
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        assert set(rel.STAGE8_TABLE_NAMES).issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()

def test_passed_rehearsal_requires_kind_specific_checkpoints(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(
            session, ids, context, task_id, rehearsal_kinds=()
        )
        rehearsal = service.start_rehearsal(
            candidate_id=candidate_id,
            rehearsal_kind="activation",
            environment_identity=DEFAULT_REHEARSAL_ENVIRONMENT,
            source_manifest_sha256=HASH_A,
            started_at=NOW + timedelta(minutes=1),
        )
        checkpoint_path, checkpoint_sha = _artifact_file("activation-writer-fence")
        service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="writer_fence",
            payload={
                "rehearsal_kind": "activation",
                "checkpoint_kind": "writer_fence",
                "evidence_kind": REHEARSAL_CHECKPOINT_EVIDENCE_KINDS["activation"]["writer_fence"],
                "artifact_identity": "fixture:activation:writer-fence",
                "artifact_path": checkpoint_path,
                "artifact_sha256": checkpoint_sha,
                "source_manifest_sha256": HASH_A,
                "environment_identity": DEFAULT_REHEARSAL_ENVIRONMENT,
                "gate_result": "pass",
            },
            recorded_at=NOW + timedelta(minutes=1),
        )
        with pytest.raises(ReleaseAuthorityError, match="lacks required checkpoints"):
            service.finish_rehearsal(
                rehearsal_id=rehearsal.rehearsal_id,
                passed=True,
                report={
                    "rehearsal_kind": "activation",
                    "source_manifest_sha256": HASH_A,
                    "environment_identity": DEFAULT_REHEARSAL_ENVIRONMENT,
                    "result": "passed",
                    "checkpoint_manifest_sha256": sha256_json(
                        [
                            {
                                "checkpoint_kind": "writer_fence",
                                "payload_sha256": session.scalar(
                                    select(rel.RehearsalCheckpoint.payload_sha256).where(
                                        rel.RehearsalCheckpoint.rehearsal_id == rehearsal.rehearsal_id
                                    )
                                ),
                            }
                        ]
                    ),
                },
                completed_at=NOW + timedelta(minutes=2),
            )

def test_writer_fence_proof_is_candidate_bound_and_pre_body_parse(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@stage8-test",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/stage8-fence.json"},
            prepared_at=NOW,
        )
        _record_and_engage_writer_fence(service, ids, fence_id=fence.fence_id, engaged_at=NOW)
        weak = _writer_fence_proof(fence, candidate_id)
        weak["http_status"] = 401
        with pytest.raises(ReleaseAuthorityError, match="exact authenticated mutation response"):
            service.verify_writer_fence(
                fence_id=fence.fence_id,
                proof=weak,
                verified_at=NOW + timedelta(minutes=1),
                required_writer_inventory={fence.target_identity},
            )
        weak = _writer_fence_proof(fence, candidate_id)
        weak["body_loaded"] = True
        with pytest.raises(ReleaseAuthorityError, match="exact authenticated mutation response"):
            service.verify_writer_fence(
                fence_id=fence.fence_id,
                proof=weak,
                verified_at=NOW + timedelta(minutes=1),
                required_writer_inventory={fence.target_identity},
            )
        assert service.writer_fence_status(fence.fence_id).state == "engaged"
        assert service.writer_fence_status(fence.fence_id).proof_sha256 is None

def test_prepared_writer_fence_digest_matches_deployed_manifest_bytes(
    workflow_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service.candidate_status(candidate_id)
        path = tmp_path / "legacy-writer-fence.json"
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@digest-bound",
            mechanism="fail-closed-file",
            manifest={"path": str(path)},
            prepared_at=NOW,
        )
        _manifest, deployed_digest = engage_legacy_writer_fence(
            path,
            fence_id=str(fence.fence_id),
            candidate_id=str(candidate_id),
            source_release=candidate.source_release,
            source_commit=candidate.source_commit,
            engaged_at=NOW,
            operator="Marco",
        )
        assert deployed_digest == fence.manifest_sha256
        observed = observe_legacy_writer_fence(
            path, expected_manifest_sha256=fence.manifest_sha256, clock=lambda: NOW
        )
        assert observed.artifact_sha256 == fence.manifest_sha256
        durable = service.record_writer_fence_artifact_observation(
            fence_id=fence.fence_id,
            artifact_generation_identity=str(candidate.generation_id),
            canonical_path=observed.observed_path,
            content_sha256=observed.artifact_sha256,
            filesystem_device=observed.device,
            filesystem_inode=observed.inode,
            verification_result="matched",
            observation_contract_version="dish-writer-fence-observation-v1",
            observed_at=observed.observed_at,
            recorded_at=observed.observed_at,
        )
        engaged = service.engage_writer_fence(
            fence_id=fence.fence_id,
            artifact_observation_id=durable.observation_id,
            engaged_at=NOW,
        )
        assert engaged.state == "engaged"


def test_writer_fence_observation_digest_canonicalizes_timestamp_to_utc(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@timezone-bound",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/timezone-bound-writer-fence.json"},
            prepared_at=NOW,
        )
        observed_at = NOW.astimezone(timezone(timedelta(hours=2)))
        observation = service.record_writer_fence_artifact_observation(
            fence_id=fence.fence_id,
            artifact_generation_identity="timezone-bound-fixture-v1",
            canonical_path="/tmp/timezone-bound-writer-fence.json",
            content_sha256=fence.manifest_sha256,
            filesystem_device=1,
            filesystem_inode=(fence.fence_id.int % 2_000_000_000) + 1,
            verification_result="matched",
            observation_contract_version="writer-fence-fixture-v1",
            observed_at=observed_at,
            recorded_at=observed_at,
        )
        assert observation.evidence_sha256 == sha256_json(
            {
                "fence_id": str(fence.fence_id),
                "candidate_id": str(candidate_id),
                "artifact_generation_identity": "timezone-bound-fixture-v1",
                "canonical_path": "/tmp/timezone-bound-writer-fence.json",
                "content_sha256": fence.manifest_sha256,
                "filesystem_device": 1,
                "filesystem_inode": (fence.fence_id.int % 2_000_000_000) + 1,
                "file_type": "regular",
                "regular_file": True,
                "verification_result": "matched",
                "observation_contract_version": "writer-fence-fixture-v1",
                "observed_at": NOW.astimezone(timezone.utc).isoformat(),
            }
        )


def test_writer_fence_engagement_rejects_deployed_manifest_digest_mismatch(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@digest-mismatch",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/digest-mismatch-writer-fence.json"},
            prepared_at=NOW,
        )
        observation = service.record_writer_fence_artifact_observation(
            fence_id=fence.fence_id,
            artifact_generation_identity="digest-mismatch-fixture-v1",
            canonical_path="/tmp/digest-mismatch-writer-fence.json",
            content_sha256="0" * 64,
            filesystem_device=1,
            filesystem_inode=(fence.fence_id.int % 2_000_000_000) + 1,
            verification_result="matched",
            observation_contract_version="writer-fence-fixture-v1",
            observed_at=NOW,
            recorded_at=NOW,
        )

        with pytest.raises(
            ReleaseAuthorityError, match="does not match the planned manifest"
        ):
            service.engage_writer_fence(
                fence_id=fence.fence_id,
                artifact_observation_id=observation.observation_id,
                engaged_at=NOW,
            )
        assert service.writer_fence_status(fence.fence_id).state == "prepared"

def test_writer_fence_inventory_requires_exact_engaged_set(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        required_fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@required",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/required-writer-fence.json"},
            prepared_at=NOW,
        )
        extra_fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@extra",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/extra-writer-fence.json"},
            prepared_at=NOW,
        )
        _record_and_engage_writer_fence(
            service, ids, fence_id=required_fence.fence_id, engaged_at=NOW
        )
        _record_and_engage_writer_fence(
            service, ids, fence_id=extra_fence.fence_id, engaged_at=NOW
        )

        with pytest.raises(
            ReleaseAuthorityError,
            match=(
                "missing_writer_targets=.*legacy-service@missing.*"
                "extra_writer_targets=.*legacy-service@extra"
            ),
        ):
            service.verify_writer_fence(
                fence_id=required_fence.fence_id,
                proof=_writer_fence_proof(required_fence, candidate_id),
                verified_at=NOW + timedelta(minutes=1),
                required_writer_inventory={
                    "legacy-service@required",
                    "legacy-service@missing",
                },
            )
        assert service.writer_fence_status(required_fence.fence_id).state == "engaged"

def test_writer_fence_inventory_must_be_supplied(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@inventory-todo",
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/inventory-todo-writer-fence.json"},
            prepared_at=NOW,
        )
        _record_and_engage_writer_fence(
            service, ids, fence_id=fence.fence_id, engaged_at=NOW
        )

        with pytest.raises(ReleaseAuthorityError, match="inventory is not configured"):
            service.verify_writer_fence(
                fence_id=fence.fence_id,
                proof=_writer_fence_proof(fence, candidate_id),
                verified_at=NOW + timedelta(minutes=1),
            )
        assert service.writer_fence_status(fence.fence_id).state == "engaged"

def test_post_burn_evidence_cannot_predate_rollback_burn(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, _cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        candidate = service.candidate_status(candidate_id)
        with pytest.raises(ReleaseAuthorityError, match="at or after rollback burn"):
            service.record_runtime_release_attestation(
                candidate_id=candidate_id,
                service_artifact_sha256="1" * 64,
                projection_worker_artifact_sha256="2" * 64,
                route_probe_sha256="3" * 64,
                payload={
                    "dish_release": candidate.dish_release,
                    "protocol_release": candidate.protocol_release,
                    "openapi_release": candidate.openapi_release,
                    "routing_release": candidate.routing_release,
                    "route_target": "postgresql",
                    "health": "pass",
                    "mutation_admission": "closed",
                "external_projection": "disabled_post_burn",
                },
                recorded_at=NOW + timedelta(minutes=5),
            )
