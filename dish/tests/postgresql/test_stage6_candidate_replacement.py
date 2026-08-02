from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, text

from dish_pg import models
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD, ReleaseCandidateService
from dish_pg.transition import ProjectionService, ShadowService, SourceImportService
from tests.support.postgresql.release import _prepare_candidate, _record_final_closure
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _replacement_authorities(session, ids, context, task_id):
    source_commit = "replacement-commit"
    replacement_import_run_id = _next(ids)
    session.add(
        models.ImportRun(
            import_run_id=replacement_import_run_id,
            source_commit=source_commit,
            source_release="dish-replacement",
            legacy_generation_id="legacy-replacement",
            baseline_high_water_mark="asana-event-replacement",
            source_bundle_sha256="d" * 64,
            status="complete",
            started_at=NOW + timedelta(minutes=10),
            completed_at=NOW + timedelta(minutes=10),
            provenance={"capture": "replacement-fixture"},
        )
    )
    session.flush()
    source = SourceImportService(session, uuid_factory=lambda: _next(ids))
    batch_id = _next(ids)
    source.start_batch(
        import_batch_id=batch_id,
        generation_id=context["generation_id"],
        import_run_id=replacement_import_run_id,
        source_release="dish-replacement",
        source_commit=source_commit,
        source_database_sha256="d" * 64,
        source_sidecars={"audit": {"sha256": "d" * 64}},
        ledger_through_commit=source_commit,
        expected_entities=1,
        started_at=NOW + timedelta(minutes=10),
    )
    source.record_entity(
        import_batch_id=batch_id,
        entity_kind="task",
        source_identity="asana:123456789",
        source_sha256="d" * 64,
        target_entity_type="dish_task",
        target_entity_id=task_id,
        provenance={"source": "replacement-fixture"},
        imported_at=NOW + timedelta(minutes=10),
    )
    source.complete_batch(
        import_batch_id=batch_id, completed_at=NOW + timedelta(minutes=10)
    )

    shadow = ShadowService(session, uuid_factory=lambda: _next(ids))
    baseline = shadow.create_baseline(
        generation_id=context["generation_id"],
        source_generation_identity="legacy-generation-replacement",
        source_commit=source_commit,
        created_at=NOW + timedelta(minutes=10),
    )
    shadow.close_baseline(
        baseline_id=baseline.shadow_baseline_id,
        closed_at=NOW + timedelta(minutes=10),
    )

    epoch = ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
        generation_id=context["generation_id"],
        activation_reason="replacement candidate",
        created_at=NOW + timedelta(minutes=10),
    )
    return batch_id, baseline.shadow_baseline_id, epoch.projection_epoch_id, source_commit


def test_pre_burn_abort_rebinds_closed_generation_control_to_replacement(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, first_candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=first_candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        service.validate_candidate(
            candidate_id=first_candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        closure = _record_final_closure(
            service,
            ids,
            first_candidate_id,
            closed_through_at=NOW + timedelta(minutes=2),
        )
        service.approve_candidate(
            candidate_id=first_candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve exact pre-burn candidate.",
            approval_payload={
                "decision": "approved",
                "final_asana_closure_id": str(closure.closure_id),
                "final_asana_closure_sha256": closure.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=2),
        )
        cutover = service.prepare_cutover(
            candidate_id=first_candidate_id,
            started_at=NOW + timedelta(minutes=3),
        )
        service.abort_cutover(
            cutover_run_id=cutover.cutover_run_id,
            reason="source changed before rollback burn",
            aborted_at=NOW + timedelta(minutes=4),
        )

        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None
        assert control.candidate_id == first_candidate_id
        assert control.state == "closed"
        assert control.control_revision == 1

        batch_id, baseline_id, epoch_id, source_commit = _replacement_authorities(
            session, ids, context, task_id
        )
        replacement_candidate_id = _next(ids)
        replacement = ReleaseCandidateService(
            session, uuid_factory=lambda: _next(ids)
        ).create_candidate(
            candidate_id=replacement_candidate_id,
            generation_id=context["generation_id"],
            source_import_batch_id=batch_id,
            shadow_baseline_id=baseline_id,
            projection_epoch_id=epoch_id,
            source_release="dish-replacement",
            source_commit=source_commit,
            ledger_through_commit=source_commit,
            schema_head=ALEMBIC_HEAD,
            dish_release="dish-pg-replacement",
            honest_release="honest-1",
            protocol_release="protocol-1",
            openapi_release="openapi-stage4",
            routing_release="routing-stage6",
            created_at=NOW + timedelta(minutes=10),
        )

        assert replacement.status == "assembling"
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None
        assert control.candidate_id == replacement_candidate_id
        assert control.state == "closed"
        assert control.opened_at is None
        assert control.control_revision == 2
        assert control.updated_at == NOW + timedelta(minutes=10)
        assert session.get(rel.ReleaseCandidate, first_candidate_id).status == "aborted"
        assert session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"]
            )
        ) is None
