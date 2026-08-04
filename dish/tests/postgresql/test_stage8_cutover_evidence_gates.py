from __future__ import annotations

import io
import runpy
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text

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
from tests.support.postgresql.workflow import NOW, _next, workflow_db
from tests.support.postgresql.release import (
    HASH_A,
    ROOT,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_final_closure,
    _writer_fence_proof,
)


def _burn_rollback(session, ids, context, task_id):
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
        closed_through_at=NOW + timedelta(minutes=5),
    )
    service.approve_candidate(
        candidate_id=candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="Marco",
        approval_statement="Approve exact candidate for Stage 8 gate tests.",
        approval_payload={
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=NOW + timedelta(minutes=5),
    )
    fence = service.prepare_writer_fence(
        candidate_id=candidate_id,
        target_identity="legacy-service@stage8-test",
        mechanism="fail-closed-file",
        manifest={"path": "/tmp/stage8-writer-fence.json"},
        prepared_at=NOW + timedelta(minutes=5),
    )
    run = service.prepare_cutover(
        candidate_id=candidate_id,
        started_at=NOW + timedelta(minutes=5),
    )
    service.engage_writer_fence(
        fence_id=fence.fence_id,
        engaged_at=NOW + timedelta(minutes=5),
    )
    service.verify_writer_fence(
        fence_id=fence.fence_id,
        proof=_writer_fence_proof(fence, candidate_id),
        verified_at=NOW + timedelta(minutes=5),
    )
    service.mark_fenced(
        cutover_run_id=run.cutover_run_id,
        recorded_at=NOW + timedelta(minutes=5),
    )
    service.recertify_candidate(
        candidate_id=candidate_id,
        closure_id=closure.closure_id,
        approver="Marco",
        recertification_statement="Confirm final closure after writer fencing.",
        payload={"cutover_run_id": str(run.cutover_run_id)},
        recertified_at=NOW + timedelta(minutes=5),
    )
    service.activate_authority(
        cutover_run_id=run.cutover_run_id,
        final_asana_closure_id=closure.closure_id,
        activated_at=NOW + timedelta(minutes=5),
    )
    service.burn_rollback(
        cutover_run_id=run.cutover_run_id,
        legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
        burned_at=NOW + timedelta(minutes=6),
    )
    return service, candidate_id, run.cutover_run_id


def _record_runtime_and_worker_readiness(session, ids, service, candidate_id, context):
    candidate = service.candidate_status(candidate_id)
    reconciliation = _complete_active_mapping_reconciliation(
        session,
        ids,
        generation_id=context["generation_id"],
        corpus_identity=f"stage8-readiness:{candidate_id}",
        started_at=NOW + timedelta(minutes=6),
        completed_at=NOW + timedelta(minutes=6),
    )
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
        },
        recorded_at=NOW + timedelta(minutes=6),
    )
    service.record_projection_worker_readiness(
        candidate_id=candidate_id,
        reconciliation_run_id=reconciliation.reconciliation_run_id,
        worker_identity="projection-worker@stage8-test",
        worker_release=candidate.dish_release,
        payload={"claim_probe": "pass", "write_probe": "pass", "restart_probe": "pass"},
        ready_at=NOW + timedelta(minutes=6),
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
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        rehearsal = service.start_rehearsal(
            candidate_id=candidate_id,
            rehearsal_kind="activation",
            environment_identity="stage8-missing-checkpoint",
            source_manifest_sha256="b" * 64,
            started_at=NOW + timedelta(minutes=1),
        )
        service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="writer_fence",
            payload={
                "rehearsal_kind": "activation",
                "checkpoint_kind": "writer_fence",
                "evidence_kind": REHEARSAL_CHECKPOINT_EVIDENCE_KINDS["activation"]["writer_fence"],
                "artifact_identity": "fixture:activation:writer-fence",
                "artifact_sha256": "c" * 64,
                "source_manifest_sha256": "b" * 64,
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
                    "source_manifest_sha256": "b" * 64,
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
        service.engage_writer_fence(fence_id=fence.fence_id, engaged_at=NOW)
        weak = _writer_fence_proof(fence, candidate_id)
        weak["http_status"] = 401
        with pytest.raises(ReleaseAuthorityError, match="exact authenticated mutation response"):
            service.verify_writer_fence(
                fence_id=fence.fence_id,
                proof=weak,
                verified_at=NOW + timedelta(minutes=1),
            )
        weak = _writer_fence_proof(fence, candidate_id)
        weak["body_loaded"] = True
        with pytest.raises(ReleaseAuthorityError, match="exact authenticated mutation response"):
            service.verify_writer_fence(
                fence_id=fence.fence_id,
                proof=weak,
                verified_at=NOW + timedelta(minutes=1),
            )
        assert service.writer_fence_status(fence.fence_id).state == "engaged"
        assert service.writer_fence_status(fence.fence_id).proof_sha256 is None


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
                },
                recorded_at=NOW + timedelta(minutes=5),
            )


def test_admission_requires_post_burn_runtime_worker_and_first_request_evidence(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        with pytest.raises(ReleaseAuthorityError, match="runtime attestation"):
            service.open_mutation_admission(
                cutover_run_id=cutover_run_id,
                opened_at=NOW + timedelta(minutes=7),
            )

        _record_runtime_and_worker_readiness(
            session, ids, service, candidate_id, context
        )
        with pytest.raises(ReleaseAuthorityError, match="first-admission plan"):
            service.open_mutation_admission(
                cutover_run_id=cutover_run_id,
                opened_at=NOW + timedelta(minutes=7),
            )

        first_request_id = _next(ids)
        plan = service.plan_first_admission(
            cutover_run_id=cutover_run_id,
            request_id=first_request_id,
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            payload={"probe": "stage8-first-admission"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        assert plan.expected_projection_events == 0
        assert plan.payload["command_arguments"]["task_id"] == str(task_id)
        control = service.open_mutation_admission(
            cutover_run_id=cutover_run_id,
            opened_at=NOW + timedelta(minutes=7),
        )
        assert control.state == "open"
        checkpoint = session.scalar(
            select(rel.CutoverCheckpoint).where(
                rel.CutoverCheckpoint.cutover_run_id == cutover_run_id,
                rel.CutoverCheckpoint.checkpoint_kind == "mutation_admission_opened",
            )
        )
        assert checkpoint is not None
        assert checkpoint.payload["runtime_attestation_id"]
        assert checkpoint.payload["projection_worker_readiness_id"]
        assert checkpoint.payload["first_admission_plan_id"]


def test_stage8_operator_cli_exposes_readiness_and_first_admission_commands() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-release"))
    parser = namespace["_parser"]()
    assert parser.parse_args([
        "runtime-attestation-record",
        "00000000-0000-0000-0000-000000000001",
        "--file",
        "/tmp/runtime.json",
    ]).command == "runtime-attestation-record"
    assert parser.parse_args([
        "projection-worker-ready",
        "00000000-0000-0000-0000-000000000001",
        "--file",
        "/tmp/worker.json",
    ]).command == "projection-worker-ready"
    assert parser.parse_args([
        "first-admission-plan",
        "00000000-0000-0000-0000-000000000002",
        "--file",
        "/tmp/first.json",
    ]).command == "first-admission-plan"


def test_first_admission_plan_rejects_unverifiable_target_shapes(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, _candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        common = {
            "cutover_run_id": cutover_run_id,
            "request_id": _next(ids),
            "payload": {"probe": "invalid-first-admission"},
            "recorded_at": NOW + timedelta(minutes=6),
        }
        with pytest.raises(ReleaseAuthorityError, match="must use the bounded start command"):
            service.plan_first_admission(
                **common,
                command_name="create",
                command_arguments={"title": "New task"},
                task_id=None,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="requires task_id"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
                task_id=None,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="must include canonical task_id"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={},
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="task identity conflicts"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={"task_id": str(_next(ids))},
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="must use the bounded start command"):
            service.plan_first_admission(
                **common,
                command_name="prepare",
                command_arguments={"task_id": str(task_id)},
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="cannot carry prior operation"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={
                    "task_id": str(task_id),
                    "agent": "codex",
                    "kind": "initial",
                    "operation_id": str(_next(ids)),
                },
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="kind must be initial"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={
                    "task_id": str(task_id),
                    "agent": "codex",
                    "kind": "planning",
                },
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="first-admission agent"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={"task_id": str(task_id), "kind": "initial"},
                task_id=task_id,
            )
