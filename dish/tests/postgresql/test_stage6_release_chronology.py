from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import hashlib
import tempfile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import (
    ALEMBIC_HEAD,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    ReleaseAuthorityError,
    ReleaseCandidateService,
    sha256_json,
)
from tests.support.postgresql.release import HASH_A, ROOT, _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _checkpoint_payload(kind: str, checkpoint_kind: str) -> dict[str, str]:
    path = Path(tempfile.mkdtemp(prefix="dish-checkpoint-chronology-")) / f"{kind}-{checkpoint_kind}.json"
    path.write_bytes(f"{kind}:{checkpoint_kind}\n".encode("utf-8"))
    return {
        "rehearsal_kind": kind,
        "checkpoint_kind": checkpoint_kind,
        "evidence_kind": REHEARSAL_CHECKPOINT_EVIDENCE_KINDS[kind][checkpoint_kind],
        "artifact_identity": f"chronology:{kind}:{checkpoint_kind}",
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_manifest_sha256": HASH_A,
        "gate_result": "pass",
    }


def test_rehearsal_chronology_rejects_naive_future_and_backward_times(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        service = ReleaseCandidateService(
            session,
            uuid_factory=lambda: _next(ids),
            clock=lambda: NOW + timedelta(minutes=30),
        )
        with pytest.raises(ReleaseAuthorityError, match="explicit timezone offset"):
            service.start_rehearsal(
                candidate_id=candidate_id,
                rehearsal_kind="activation",
                environment_identity="naive-time",
                source_manifest_sha256=HASH_A,
                started_at=(NOW + timedelta(minutes=1)).replace(tzinfo=None),
            )
        with pytest.raises(ReleaseAuthorityError, match="trusted database clock"):
            service.start_rehearsal(
                candidate_id=candidate_id,
                rehearsal_kind="activation",
                environment_identity="future-time",
                source_manifest_sha256=HASH_A,
                started_at=NOW + timedelta(minutes=31),
            )

        rehearsal = service.start_rehearsal(
            candidate_id=candidate_id,
            rehearsal_kind="activation",
            environment_identity="ordered-time",
            source_manifest_sha256=HASH_A,
            started_at=NOW + timedelta(minutes=10),
        )
        with pytest.raises(ReleaseAuthorityError, match="rehearsal started_at"):
            service.record_rehearsal_checkpoint(
                rehearsal_id=rehearsal.rehearsal_id,
                checkpoint_kind="writer_fence",
                payload=_checkpoint_payload("activation", "writer_fence"),
                recorded_at=NOW + timedelta(minutes=9),
            )
        first = service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="writer_fence",
            payload=_checkpoint_payload("activation", "writer_fence"),
            recorded_at=NOW + timedelta(minutes=11),
        )
        with pytest.raises(ReleaseAuthorityError, match="prior rehearsal checkpoint"):
            service.record_rehearsal_checkpoint(
                rehearsal_id=rehearsal.rehearsal_id,
                checkpoint_kind="authority_activation",
                payload=_checkpoint_payload("activation", "authority_activation"),
                recorded_at=NOW + timedelta(minutes=10),
            )
        second = service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="authority_activation",
            payload=_checkpoint_payload("activation", "authority_activation"),
            recorded_at=NOW + timedelta(minutes=12),
        )
        third = service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="rollback_burn",
            payload=_checkpoint_payload("activation", "rollback_burn"),
            recorded_at=NOW + timedelta(minutes=13),
        )
        fourth = service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="first_admission",
            payload=_checkpoint_payload("activation", "first_admission"),
            recorded_at=NOW + timedelta(minutes=14),
        )
        checkpoints = [
            {"checkpoint_kind": checkpoint.checkpoint_kind, "payload_sha256": checkpoint.payload_sha256}
            for checkpoint in (first, second, third, fourth)
        ]
        report = {
            "rehearsal_kind": "activation",
            "source_manifest_sha256": HASH_A,
            "result": "passed",
            "checkpoint_manifest_sha256": sha256_json(checkpoints),
        }
        with pytest.raises(ReleaseAuthorityError, match="checkpoint authority_activation"):
            service.finish_rehearsal(
                rehearsal_id=rehearsal.rehearsal_id,
                passed=True,
                report=report,
                completed_at=NOW + timedelta(minutes=11),
            )
        finished = service.finish_rehearsal(
            rehearsal_id=rehearsal.rehearsal_id,
            passed=True,
            report=report,
            completed_at=NOW + timedelta(minutes=15),
        )
        assert finished.status == "passed"


def test_candidate_validation_cannot_predate_candidate_evidence_or_bundle(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        with pytest.raises(ReleaseAuthorityError, match="candidate created_at"):
            service.build_evidence_bundle(
                candidate_id=candidate_id,
                bundle_kind="release_candidate",
                built_at=NOW - timedelta(seconds=1),
            )
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        with pytest.raises(ReleaseAuthorityError, match="candidate created_at"):
            service.validate_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=bundle.bundle_id,
                validated_at=NOW - timedelta(seconds=1),
            )


def test_final_closure_rejects_naive_operator_time(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
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
        with pytest.raises(ReleaseAuthorityError, match="explicit timezone offset"):
            service.record_final_asana_closure(
                candidate_id=candidate_id,
                capture_manifest_sha256=HASH_A,
                observation_high_water="asana-change-naive",
                watcher_identity="chronology-test",
                interval_started_at=NOW.replace(tzinfo=None),
                closed_through_at=NOW + timedelta(minutes=2),
                payload={"tasks": 1},
                recorded_at=NOW + timedelta(minutes=2),
            )


def test_rehearsal_model_rejects_completion_before_start(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        session.add(
            rel.RehearsalRun(
                rehearsal_id=_next(ids),
                candidate_id=candidate_id,
                rehearsal_kind="activation",
                environment_identity="invalid-durable-time",
                source_manifest_sha256=HASH_A,
                status="failed",
                run_revision=2,
                report={"result": "failed"},
                report_sha256=HASH_A,
                measured_rpo_seconds=None,
                measured_rto_seconds=None,
                started_at=NOW + timedelta(minutes=20),
                completed_at=NOW + timedelta(minutes=19),
            )
        )
        with pytest.raises(IntegrityError, match="completion_not_before_start"):
            session.flush()


@pytest.mark.database_boundary
def test_release_chronology_migration_adds_rehearsal_ordering_constraint(tmp_path: Path) -> None:
    path = tmp_path / "release-chronology.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "head")

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        checks = {
            row["name"]: row["sqltext"]
            for row in inspect(engine).get_check_constraints("rehearsal_runs")
        }
        assert "ck_rehearsal_runs_completion_not_before_start" in checks
        assert "completed_at" in checks["ck_rehearsal_runs_completion_not_before_start"]
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()
