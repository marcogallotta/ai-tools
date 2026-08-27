from __future__ import annotations

import io
import hashlib
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import (
    ALEMBIC_HEAD,
    EVIDENCE_ARTIFACT_KINDS,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    REQUIRED_EVIDENCE,
    REQUIRED_REHEARSALS,
    REQUIRED_REHEARSAL_CHECKPOINTS,
    ReleaseAuthorityError,
    ReleaseCandidateService,
)
from dish_pg.transition import ProjectionService, ShadowService, SourceImportService
from dish_pg.workflow import (
    ExecutionSpec,
    MutationAdmissionClosed,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
    sha256_json,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import (
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_final_closure,
    _writer_fence_proof,
)

ROOT = Path(__file__).resolve().parents[2]
HASH_A = "a" * 64


@pytest.mark.database_boundary
def test_stage6_schema_migration_and_postgresql_guards(tmp_path: Path) -> None:
    assert set(rel.STAGE6_TABLE_NAMES).issubset(models.Base.metadata.tables)
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, "head", sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE release_candidates" in rendered
    assert "CREATE TABLE cutover_runs" in rendered
    assert "dish_validate_release_candidate_transition" in rendered
    assert "dish_require_open_mutation_admission" in rendered
    assert "WITH RECURSIVE archived_lineage" in rendered
    assert "child.completion_reason='imported'" in rendered

    path = tmp_path / "stage6.sqlite3"
    online = Config(str(ROOT / "alembic.ini"))
    online.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(online, "head")
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        assert set(rel.STAGE6_TABLE_NAMES).issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()


def test_candidate_evaluation_bundle_is_deterministic_and_stale_safe(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        evaluation = service.evaluate_candidate(candidate_id=candidate_id)
        assert evaluation.passed, [check.as_dict() for check in evaluation.checks if not check.passed]
        first = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        second = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW + timedelta(hours=1),
        )
        assert second.bundle_id == first.bundle_id
        replacement_path = Path(tempfile.mkdtemp(prefix="dish-release-replacement-")) / "replacement.json"
        replacement_path.write_bytes(b"replacement\n")
        service.record_evidence(
            candidate_id=candidate_id,
            category="authority_coverage",
            evidence_key="current_to_target",
            outcome="pass",
            payload={
                "artifact_kind": EVIDENCE_ARTIFACT_KINDS[("authority_coverage", "current_to_target")],
                "artifact_identity": "fixture:authority-coverage:replacement",
                "artifact_path": str(replacement_path),
                "artifact_sha256": hashlib.sha256(replacement_path.read_bytes()).hexdigest(),
                "source_manifest_sha256": HASH_A,
                "gate_name": "authority_coverage:current_to_target",
                "gate_result": "pass",
            },
            recorded_at=NOW + timedelta(minutes=1),
        )
        with pytest.raises(ReleaseAuthorityError, match="stale"):
            service.validate_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=first.bundle_id,
                validated_at=NOW + timedelta(minutes=2),
            )
        current = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW + timedelta(minutes=2),
        )
        assert current.bundle_id != first.bundle_id
        assert service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=current.bundle_id,
            validated_at=NOW + timedelta(minutes=3),
        ).passed


def test_acceptance_fails_closed_on_unresolved_authority_and_incomplete_mapping_reconciliation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        # An unresolved request predating candidate creation must block closure.
        # Candidate creation itself installs the closed admission control.
        run_id, request_id = _next(ids), _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)).admit_request(
            RequestSpec(
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner_id="owner-1",
                principal_class="agent",
                command_name="start",
                canonical_payload={"arguments": {"task_id": str(task_id)}},
                protocol_release="protocol-1",
                dish_release="dish-42619b9",
                admitted_at=NOW,
            )
        )
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        result = service.evaluate_candidate(candidate_id=candidate_id)
        failed = {check.code: check.details for check in result.checks if not check.passed}
        assert failed["legacy_and_target_authority_resolved"]["requests_without_outcome"] == 1
        assert failed["quiescent_cutover_authority"]["authority_requests_without_outcome"] == 1

        # A complete reconciliation must account for every active mapping, not
        # merely declare a smaller corpus complete.
        latest = session.scalar(
            select(tx.ProjectionReconciliationRun).where(
                tx.ProjectionReconciliationRun.generation_id == context["generation_id"]
            )
        )
        latest.expected_items = 2
        latest.processed_items = 2
        result = service.evaluate_candidate(candidate_id=candidate_id)
        projection = next(check for check in result.checks if check.code == "projection_ready")
        assert not projection.passed
        assert projection.details["active_mappings"] == 3


def test_release_evidence_is_database_immutable(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        evidence_id = session.scalar(
            select(rel.ReleaseEvidenceItem.evidence_id).where(
                rel.ReleaseEvidenceItem.candidate_id == candidate_id
            ).limit(1)
        )
    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            session.execute(
                update(rel.ReleaseEvidenceItem)
                .where(rel.ReleaseEvidenceItem.evidence_id == evidence_id)
                .values(outcome="fail")
            )
