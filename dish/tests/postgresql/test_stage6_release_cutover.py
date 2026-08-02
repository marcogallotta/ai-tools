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
            payload={
                "artifact_kind": EVIDENCE_ARTIFACT_KINDS[(category, key)],
                "artifact_identity": f"fixture:{category}:{key}",
                "artifact_path": f"/evidence/{category}/{key}.json",
                "artifact_sha256": HASH_A,
                "source_manifest_sha256": HASH_A,
                "gate_name": f"{category}:{key}",
                "gate_result": "pass",
            },
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
        checkpoints = []
        for checkpoint_kind in REQUIRED_REHEARSAL_CHECKPOINTS[kind]:
            checkpoint = service.record_rehearsal_checkpoint(
                rehearsal_id=rehearsal.rehearsal_id,
                checkpoint_kind=checkpoint_kind,
                payload={
                    "rehearsal_kind": kind,
                    "checkpoint_kind": checkpoint_kind,
                    "evidence_kind": REHEARSAL_CHECKPOINT_EVIDENCE_KINDS[kind][checkpoint_kind],
                    "artifact_identity": f"fixture:{kind}:{checkpoint_kind}",
                    "artifact_sha256": HASH_A,
                    "source_manifest_sha256": HASH_A,
                    "gate_result": "pass",
                },
                recorded_at=NOW,
            )
            checkpoints.append(
                {
                    "checkpoint_kind": checkpoint.checkpoint_kind,
                    "payload_sha256": checkpoint.payload_sha256,
                }
            )
        service.finish_rehearsal(
            rehearsal_id=rehearsal.rehearsal_id,
            passed=True,
            report={
                "rehearsal_kind": kind,
                "source_manifest_sha256": HASH_A,
                "result": "passed",
                "checkpoint_manifest_sha256": sha256_json(checkpoints),
            },
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


def _writer_fence_proof(fence, candidate_id):
    return {
        "probe_kind": "authenticated_mutation_rejected_before_body_parse",
        "candidate_id": str(candidate_id),
        "target_identity": fence.target_identity,
        "fence_manifest_sha256": fence.manifest_sha256,
        "request_token_sha256": "f" * 64,
        "http_status": 409,
        "response_code": "CONFLICT",
        "response_rule": "legacy_writer_fenced",
        "response_retryable": False,
        "body_loaded": False,
        "result": "pass",
    }


def _complete_active_mapping_reconciliation(
    session,
    ids,
    *,
    generation_id,
    corpus_identity: str,
    started_at,
    completed_at,
):
    active_mappings: list[tuple[str, object]] = []
    for mapping_model, entity_kind in (
        (tx.ProjectProjectionMapping, "project"),
        (tx.SectionProjectionMapping, "section"),
        (tx.TaskProjectionMapping, "task"),
    ):
        rows = session.scalars(
            select(mapping_model).where(
                mapping_model.generation_id == generation_id,
                mapping_model.state == "active",
            )
        ).all()
        active_mappings.extend((entity_kind, row) for row in rows)

    projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
    run = projection.start_reconciliation(
        generation_id=generation_id,
        corpus_identity=corpus_identity,
        expected_items=len(active_mappings),
        started_at=started_at,
    )
    for entity_kind, mapping in active_mappings:
        projection.record_reconciliation_item(
            reconciliation_run_id=run.reconciliation_run_id,
            item_identity=f"{entity_kind}:{mapping.mapping_id}",
            entity_kind=entity_kind,
            mapping_id=mapping.mapping_id,
            outcome="matched",
            evidence={"source": corpus_identity},
            recorded_at=started_at,
        )
    projection.complete_reconciliation(
        reconciliation_run_id=run.reconciliation_run_id,
        completed_at=completed_at,
    )
    return run


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
            payload={
                "artifact_kind": EVIDENCE_ARTIFACT_KINDS[("authority_coverage", "current_to_target")],
                "artifact_identity": "fixture:authority-coverage:replacement",
                "artifact_path": "/evidence/authority_coverage/replacement.json",
                "artifact_sha256": "b" * 64,
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
            approved_at=NOW + timedelta(minutes=5),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW + timedelta(minutes=5),
        )
        cutover = service.prepare_cutover(
            candidate_id=candidate_id, started_at=NOW + timedelta(minutes=5)
        )
        cutover_id, fence_id = cutover.cutover_run_id, fence.fence_id

    # Resume each irreversible step in a fresh transaction/service instance.
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.engage_writer_fence(fence_id=fence_id, engaged_at=NOW + timedelta(minutes=5))
        service.verify_writer_fence(
            fence_id=fence_id,
            proof=_writer_fence_proof(
                service._fence(fence_id), candidate_id
            ),
            verified_at=NOW + timedelta(minutes=5),
        )
        service.mark_fenced(cutover_run_id=cutover_id, recorded_at=NOW + timedelta(minutes=5))
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

    first_request_id = _next(ids)
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        activation = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=6),
        )
        assert activation.rollback_burned_at is not None
        candidate = service._candidate(candidate_id)
        readiness_reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="projection-worker-readiness",
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
            reconciliation_run_id=readiness_reconciliation.reconciliation_run_id,
            worker_identity="projection-worker@fixture",
            worker_release=candidate.dish_release,
            payload={"claim_probe": "pass", "write_probe": "pass", "restart_probe": "pass"},
            ready_at=NOW + timedelta(minutes=6),
        )
        service.plan_first_admission(
            cutover_run_id=cutover_id,
            request_id=first_request_id,
            command_name="start",
            task_id=task_id,
            expected_projection_events=0,
            payload={"probe": "first production mutation"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        control = service.open_mutation_admission(
            cutover_run_id=cutover_id, opened_at=NOW + timedelta(minutes=7)
        )
        assert control.state == "open"

    with session_scope(factory) as session:
        run_id, request_id = _next(ids), first_request_id
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
        binding_id = _next(ids)
        session.add(
            models.HonestContractBinding(
                binding_id=binding_id,
                binding_kind="release",
                source_identity="honest-pantry@stage6-first-admission",
                dish_release="dish-pg-stage6",
                honest_release="honest-1",
                protocol_release="protocol-1",
                protocol_sha256=HASH_A,
                schema_release="schema-1",
                schema_sha256="b" * 64,
                migration_id=None,
                source_schema_version=None,
                target_schema_version=None,
                migration_metadata_sha256=None,
                source_ids={"route": "first-admission"},
                provenance={"source": "cutover-test"},
                resolved_at=NOW + timedelta(minutes=8),
            )
        )
        session.flush()
        execution_id = _next(ids)
        workflow.begin_execution(
            ExecutionSpec(
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=None,
                command_name="start",
                transaction_profile="L",
                canonical_intent={"command": "start", "task_id": str(task_id)},
                pinned_inputs={"now": (NOW + timedelta(minutes=8)).isoformat()},
                contract_binding_id=binding_id,
                admitted_at=NOW + timedelta(minutes=8),
            )
        )
        payload = {"ok": True, "first_admission": True}
        workflow.repo.record_outcome(
            request_id=request_id,
            outcome=StoredOutcome(
                outcome_id=_next(ids),
                outcome_class="success",
                result_code="OK",
                http_status=200,
                result_payload=payload,
                immutable_success=True,
                recorded_at=NOW + timedelta(minutes=8),
            ),
            execution_id=execution_id,
            audit_event_id=_next(ids),
            audit_event_type="first_production_admission",
            actor="owner-1",
            audit_payload={"cutover_run_id": str(cutover_id)},
            task_id=task_id,
            operation_id=None,
            obligation_id=_next(ids),
            invocation_metadata={"surface": "production-cutover"},
        )
        obligation = session.scalar(
            select(wf.InvocationAuditObligation).where(
                wf.InvocationAuditObligation.request_id == request_id
            )
        )
        assert obligation is not None
        obligation.state = "fulfilled"
        obligation.terminal_at = NOW + timedelta(minutes=8)

        post = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="post-first-admission",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        with pytest.raises(ReleaseAuthorityError, match="execution, audit, projection, and reconciliation"):
            service.verify_first_admission(
                cutover_run_id=cutover_id,
                request_id=request_id,
                verified_at=NOW + timedelta(minutes=8),
            )
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
        assert final_bundle.manifest["acceptance"]["passed"], [c for c in final_bundle.manifest["acceptance"]["checks"] if not c["passed"]]
        with pytest.raises(ReleaseAuthorityError, match="prohibited"):
            service.abort_cutover(
                cutover_run_id=cutover_id,
                reason="too late",
                aborted_at=NOW + timedelta(minutes=11),
            )
