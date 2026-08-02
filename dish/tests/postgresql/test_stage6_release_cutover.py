from __future__ import annotations

import io
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
    REQUIRED_EVIDENCE,
    REQUIRED_REHEARSALS,
    ReleaseAuthorityError,
    ReleaseCandidateService,
)
from dish_pg.transition import ProjectionService, ShadowService, SourceImportService
from dish_pg.workflow import MutationAdmissionClosed, RequestSpec, WorkflowAuthorityService, sha256_json
from tests.postgresql.test_stage3_workflow_authority import NOW, _next, _register_run, workflow_db

ROOT = Path(__file__).resolve().parents[2]
HASH_A = "a" * 64


def _prepare_candidate(session, ids, context, task_id):
    # Base.metadata fixtures do not normally carry Alembic's version table.
    session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(64) NOT NULL)"))
    session.execute(text("DELETE FROM alembic_version"))
    session.execute(text("INSERT INTO alembic_version(version_num) VALUES (:head)"), {"head": ALEMBIC_HEAD})

    source = SourceImportService(session, uuid_factory=lambda: _next(ids))
    batch_id = _next(ids)
    source.start_batch(
        import_batch_id=batch_id,
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        source_release="dish-42619b9",
        source_commit="42619b9",
        source_database_sha256=HASH_A,
        source_sidecars={"audit": {"sha256": HASH_A}},
        ledger_through_commit="42619b9",
        expected_entities=1,
        started_at=NOW,
    )
    source.record_entity(
        import_batch_id=batch_id,
        entity_kind="task",
        source_identity="asana:123456789",
        source_sha256=HASH_A,
        target_entity_type="dish_task",
        target_entity_id=task_id,
        provenance={"source": "stage6-fixture"},
        imported_at=NOW,
    )
    source.complete_batch(import_batch_id=batch_id, completed_at=NOW)

    shadow = ShadowService(session, uuid_factory=lambda: _next(ids))
    baseline = shadow.create_baseline(
        generation_id=context["generation_id"],
        source_generation_identity="legacy-generation-1",
        source_commit="42619b9",
        created_at=NOW,
    )
    shadow.close_baseline(baseline_id=baseline.shadow_baseline_id, closed_at=NOW)

    projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
    epoch = projection.activate_epoch(
        generation_id=context["generation_id"], activation_reason="stage6 rehearsal", created_at=NOW
    )
    assert projection.bind_imported_mappings(
        generation_id=context["generation_id"], bound_at=NOW
    ) == (1, 1, 1)
    mappings = []
    for mapping_model, kind in (
        (tx.ProjectProjectionMapping, "project"),
        (tx.SectionProjectionMapping, "section"),
        (tx.TaskProjectionMapping, "task"),
    ):
        mapping = session.scalar(
            select(mapping_model).where(
                mapping_model.generation_id == context["generation_id"],
                mapping_model.projection_epoch_id == epoch.projection_epoch_id,
                mapping_model.state == "active",
            )
        )
        mappings.append((mapping, kind))
    reconciliation = projection.start_reconciliation(
        generation_id=context["generation_id"],
        corpus_identity="production-corpus@42619b9",
        expected_items=len(mappings),
        started_at=NOW,
    )
    for mapping, kind in mappings:
        projection.record_reconciliation_item(
            reconciliation_run_id=reconciliation.reconciliation_run_id,
            item_identity=f"{kind}:{mapping.mapping_id}",
            entity_kind=kind,
            mapping_id=mapping.mapping_id,
            outcome="matched",
            evidence={"reread": "exact"},
            recorded_at=NOW,
        )
    projection.complete_reconciliation(
        reconciliation_run_id=reconciliation.reconciliation_run_id, completed_at=NOW
    )

    candidate_id = _next(ids)
    service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
    service.create_candidate(
        candidate_id=candidate_id,
        generation_id=context["generation_id"],
        source_import_batch_id=batch_id,
        shadow_baseline_id=baseline.shadow_baseline_id,
        projection_epoch_id=epoch.projection_epoch_id,
        source_release="dish-42619b9",
        source_commit="42619b9",
        ledger_through_commit="42619b9",
        schema_head=ALEMBIC_HEAD,
        dish_release="dish-pg-stage6",
        honest_release="honest-1",
        protocol_release="protocol-1",
        openapi_release="openapi-stage4",
        routing_release="routing-stage6",
        created_at=NOW,
    )
    for category, key in REQUIRED_EVIDENCE:
        service.record_evidence(
            candidate_id=candidate_id,
            category=category,
            evidence_key=key,
            outcome="pass",
            payload={"result": "pass", "source": f"{category}:{key}"},
            recorded_at=NOW,
        )
    for kind in REQUIRED_REHEARSALS:
        rehearsal = service.start_rehearsal(
            candidate_id=candidate_id,
            rehearsal_kind=kind,
            environment_identity="production-shaped-fixture",
            source_manifest_sha256=HASH_A,
            started_at=NOW,
        )
        service.record_rehearsal_checkpoint(
            rehearsal_id=rehearsal.rehearsal_id,
            checkpoint_kind="completed_checks",
            payload={"kind": kind, "passed": True},
            recorded_at=NOW,
        )
        service.finish_rehearsal(
            rehearsal_id=rehearsal.rehearsal_id,
            passed=True,
            report={"kind": kind, "result": "pass"},
            measured_rpo_seconds=0.0 if kind == "restore" else None,
            measured_rto_seconds=12.5 if kind == "restore" else None,
            completed_at=NOW,
        )
    return service, candidate_id


def _record_final_closure(service, ids, candidate_id, *, closed_through_at):
    return service.record_final_asana_closure(
        candidate_id=candidate_id,
        capture_manifest_sha256=HASH_A,
        observation_high_water="asana-change-900",
        watcher_identity="final-asana-watcher@fixture",
        interval_started_at=NOW,
        closed_through_at=closed_through_at,
        payload={"tasks": 1, "registry": "closed"},
        recorded_at=closed_through_at,
    )


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
        service.record_evidence(
            candidate_id=candidate_id,
            category="authority_coverage",
            evidence_key="current_to_target",
            outcome="pass",
            payload={"result": "pass", "source": "new exact report"},
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
                canonical_payload={"task_id": str(task_id)},
                protocol_release="protocol-1",
                dish_release="dish-42619b9",
                admitted_at=NOW,
            )
        )
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        result = service.evaluate_candidate(candidate_id=candidate_id)
        failed = {check.code: check.details for check in result.checks if not check.passed}
        assert failed["legacy_and_target_authority_resolved"]["requests_without_outcome"] == 1

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


def test_cutover_is_resumable_admission_stays_closed_until_burn_and_first_outcome(workflow_db) -> None:
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
        closure = _record_final_closure(
            service, ids, candidate_id, closed_through_at=NOW + timedelta(minutes=5)
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve this exact candidate and evidence bundle.",
            approval_payload={
                "decision": "approved",
                "final_asana_closure_id": str(closure.closure_id),
                "final_asana_closure_sha256": closure.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=2),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW,
        )
        cutover = service.prepare_cutover(candidate_id=candidate_id, started_at=NOW)
        cutover_id, fence_id = cutover.cutover_run_id, fence.fence_id

    # Resume each irreversible step in a fresh transaction/service instance.
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.engage_writer_fence(fence_id=fence_id, engaged_at=NOW + timedelta(minutes=3))
        service.verify_writer_fence(
            fence_id=fence_id,
            proof={"probe": "valid token rejected before body parse"},
            verified_at=NOW + timedelta(minutes=4),
        )
        service.mark_fenced(cutover_run_id=cutover_id, recorded_at=NOW + timedelta(minutes=4))
        service.activate_authority(
            cutover_run_id=cutover_id,
            final_asana_closure_id=closure.closure_id,
            activated_at=NOW + timedelta(minutes=5),
        )

    # A validated/approved candidate cannot admit a target mutation yet.
    with pytest.raises(MutationAdmissionClosed, match="admission is closed"):
        with session_scope(factory) as session:
            run_id = _next(ids)
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)).admit_request(
                RequestSpec(
                    request_id=_next(ids),
                    generation_id=context["generation_id"],
                    run_id=run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="start",
                    canonical_payload={"task_id": str(task_id)},
                    protocol_release="protocol-1",
                    dish_release="dish-42619b9",
                    admitted_at=NOW + timedelta(minutes=5),
                )
            )

    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        activation = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=6),
        )
        assert activation.rollback_burned_at is not None
        control = service.open_mutation_admission(
            cutover_run_id=cutover_id, opened_at=NOW + timedelta(minutes=7)
        )
        assert control.state == "open"

    with session_scope(factory) as session:
        run_id, request_id = _next(ids), _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        workflow.admit_request(
            RequestSpec(
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner_id="owner-1",
                principal_class="agent",
                command_name="start",
                canonical_payload={"task_id": str(task_id)},
                protocol_release="protocol-1",
                dish_release="dish-pg-stage6",
                admitted_at=NOW + timedelta(minutes=8),
            )
        )
        payload = {"ok": True, "first_admission": True}
        session.add(
            wf.ServiceRequestOutcome(
                outcome_id=_next(ids),
                request_id=request_id,
                outcome_class="success",
                result_code="OK",
                http_status=200,
                result_payload=payload,
                result_sha256=sha256_json(payload),
                immutable_success=True,
                recorded_at=NOW + timedelta(minutes=8),
            )
        )
        session.flush()
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.verify_first_admission(
            cutover_run_id=cutover_id,
            request_id=request_id,
            verified_at=NOW + timedelta(minutes=9),
        )
        completed = service.complete_cutover(
            cutover_run_id=cutover_id, completed_at=NOW + timedelta(minutes=10)
        )
        assert completed.state == "completed"
        final_bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="cutover_final",
            built_at=NOW + timedelta(minutes=10),
        )
        assert final_bundle.manifest["activation"] is not None
        assert final_bundle.manifest["acceptance"]["passed"]
        with pytest.raises(ReleaseAuthorityError, match="prohibited"):
            service.abort_cutover(
                cutover_run_id=cutover_id,
                reason="too late",
                aborted_at=NOW + timedelta(minutes=11),
            )
