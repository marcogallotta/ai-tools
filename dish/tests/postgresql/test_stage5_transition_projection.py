from __future__ import annotations

import io
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import (
    ProjectionService,
    ShadowService,
    SourceImportService,
    TransitionAuthorityError,
)
from dish_pg.workflow import WorkflowAuthorityService
from tests.postgresql.test_stage2_core_authority import _import_one
from tests.postgresql.test_stage3_workflow_authority import (
    NOW,
    _admit,
    _execution,
    _next,
    _register_run,
    workflow_db,
)

ROOT = Path(__file__).resolve().parents[2]
SECRET = b"stage-5-cursor-secret-32-bytes!!"
HASH_A = "a" * 64


def _projection(session, ids) -> ProjectionService:
    return ProjectionService(session, uuid_factory=lambda: _next(ids))


def _claimed_execution(session, ids, context, task_id, *, command_name="prepare"):
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    _register_run(session, generation_id=context["generation_id"], run_id=run_id)
    workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
    _admit(
        workflow,
        request_id=request_id,
        generation_id=context["generation_id"],
        run_id=run_id,
        command=command_name,
        payload={"task_id": str(task_id)},
    )
    _execution(
        workflow,
        execution_id=execution_id,
        request_id=request_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        binding_id=context["binding_id"],
        command=command_name,
    )
    workflow.repo.claim_execution(
        execution_id=execution_id,
        claimant=f"owner-1:{run_id}",
        claim_token=_next(ids),
        now=NOW,
        ttl=timedelta(minutes=2),
    )
    return execution_id


def test_stage5_schema_and_migration_reach_transition_head(tmp_path: Path) -> None:
    assert set(tx.STAGE5_TABLE_NAMES).issubset(models.Base.metadata.tables)
    assert {
        "source_import_batches",
        "shadow_envelopes",
        "projection_epochs",
        "projection_outbox_events",
        "projection_attempts",
        "projection_create_correlations",
        "projection_drift_events",
        "projection_reconciliation_runs",
    }.issubset(tx.STAGE5_TABLE_NAMES)

    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, "0004_transition_projection", sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE source_import_batches" in rendered
    assert "CREATE TABLE shadow_envelopes" in rendered
    assert "CREATE TABLE projection_outbox_events" in rendered
    assert "dish_reject_immutable_transition_authority" in rendered
    assert "dish_validate_projection_epoch_generation" in rendered

    path = tmp_path / "stage5.sqlite3"
    online = Config(str(ROOT / "alembic.ini"))
    online.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(online, "0004_transition_projection")
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        assert set(tx.STAGE5_TABLE_NAMES).issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0004_transition_projection"
            )
    finally:
        engine.dispose()


def test_source_import_closes_only_with_exact_immutable_provenance(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    batch_id = _next(ids)
    with session_scope(factory) as session:
        service = SourceImportService(session, uuid_factory=lambda: _next(ids))
        batch = service.start_batch(
            import_batch_id=batch_id,
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            source_release="dish-42619b9",
            source_commit="42619b9",
            source_database_sha256=HASH_A,
            source_sidecars={"audit": "audit.jsonl"},
            ledger_through_commit="42619b9",
            expected_entities=1,
            started_at=NOW,
        )
        evidence = service.record_entity(
            import_batch_id=batch_id,
            entity_kind="task",
            source_identity="asana:123456789",
            source_sha256=HASH_A,
            target_entity_type="dish_task",
            target_entity_id=task_id,
            provenance={"table": "tasks", "rowid": 1},
            imported_at=NOW,
        )
        duplicate = service.record_entity(
            import_batch_id=batch_id,
            entity_kind="task",
            source_identity="asana:123456789",
            source_sha256=HASH_A,
            target_entity_type="dish_task",
            target_entity_id=task_id,
            provenance={"table": "tasks", "rowid": 1},
            imported_at=NOW,
        )
        assert duplicate.evidence_id == evidence.evidence_id
        assert batch.imported_entities == 1
        assert service.complete_batch(import_batch_id=batch_id, completed_at=NOW).status == "complete"

    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            session.execute(
                update(tx.SourceImportEntityEvidence)
                .where(tx.SourceImportEntityEvidence.import_batch_id == batch_id)
                .values(source_identity="changed")
            )


def test_shadow_delivery_is_resumable_and_baseline_closes_only_after_gap_resolution(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-generation-1",
            source_commit="42619b9",
            created_at=NOW,
        )
        envelope = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="prepare",
            source_request_identity="request-1",
            canonical_input={"task": "123456789"},
            source_outcome={"ok": True, "phase": "prepared"},
            source_post_state={"section": "verification"},
            captured_at=NOW,
        )
        token = _next(ids)
        delivery = service.claim_delivery(
            worker_id="shadow-1", claim_token=token, now=NOW, ttl=timedelta(minutes=2)
        )
        assert delivery is not None and delivery.envelope_id == envelope.envelope_id
        comparison = service.compare_delivery(
            delivery_id=delivery.delivery_id,
            claim_token=token,
            target_result={"ok": True, "phase": "different"},
            comparator_release="dish-pg-stage5",
            compared_at=NOW,
        )
        assert comparison.parity_class == "mismatch"
        gap = session.scalar(
            select(tx.ShadowGap).where(tx.ShadowGap.shadow_baseline_id == baseline.shadow_baseline_id)
        )
        with pytest.raises(TransitionAuthorityError, match="unresolved"):
            service.close_baseline(baseline_id=baseline.shadow_baseline_id, closed_at=NOW)
        service.resolve_gap(gap_id=gap.gap_id, resolution={"accepted": "known semantic delta"}, resolved_at=NOW)
        assert service.close_baseline(baseline_id=baseline.shadow_baseline_id, closed_at=NOW).status == "closed"


def test_imported_mappings_bind_once_to_the_active_epoch(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service = _projection(session, ids)
        epoch = service.activate_epoch(
            generation_id=context["generation_id"], activation_reason="stage5 test", created_at=NOW
        )
        assert service.bind_imported_mappings(
            generation_id=context["generation_id"], bound_at=NOW
        ) == (1, 1, 1)
        assert service.bind_imported_mappings(
            generation_id=context["generation_id"], bound_at=NOW
        ) == (0, 0, 0)
        mapping = session.scalar(
            select(tx.TaskProjectionMapping).where(tx.TaskProjectionMapping.task_id == task_id)
        )
        assert mapping.projection_epoch_id == epoch.projection_epoch_id
        assert mapping.state == "active"


def test_projection_outbox_is_idempotent_and_preserves_per_task_order(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service = _projection(session, ids)
        service.activate_epoch(
            generation_id=context["generation_id"], activation_reason="ordering", created_at=NOW
        )
        execution_id = _claimed_execution(session, ids, context, task_id)
        first_id = service.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "v2"},
            created_at=NOW,
        )
        duplicate_id = service.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "v2"},
            created_at=NOW,
        )
        second_id = service.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="move_task",
            payload={"section_id": str(context["section_id"])},
            created_at=NOW,
        )
        assert duplicate_id == first_id
        first_claim = service.claim_next(worker_id="projector-1", now=NOW, ttl=timedelta(minutes=2))
        assert first_claim and first_claim.event_id == first_id and first_claim.aggregate_sequence == 1
        assert service.claim_next(worker_id="projector-2", now=NOW, ttl=timedelta(minutes=2)) is None
        attempt = service.begin_attempt(
            event_id=first_claim.event_id,
            claim_token=first_claim.claim_token,
            worker_id="projector-1",
            request_identity="asana-write-1",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW,
        )
        first_event = session.get(tx.ProjectionOutboxEvent, first_id)
        result = service.record_observation_and_adjudicate(
            attempt_id=attempt.attempt_id,
            observation_kind="reread",
            observed_applied=True,
            observed_identity=first_event.idempotency_key,
            reread_complete=True,
            evidence={"gid": "123456789"},
            decided_by="automatic",
            decision_reason="exact reread",
            observed_at=NOW,
        )
        assert result.outcome == "confirmed"
        second_claim = service.claim_next(worker_id="projector-2", now=NOW, ttl=timedelta(minutes=2))
        assert second_claim and second_claim.event_id == second_id and second_claim.aggregate_sequence == 2


def test_lost_create_response_binds_one_marker_and_blocks_multiple_matches(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        projection = _projection(session, ids)
        projection.activate_epoch(
            generation_id=context["generation_id"], activation_reason="create correlation", created_at=NOW
        )
        port = PostgresCommandPort(
            session,
            cursor_secret=SECRET,
            uuid_factory=lambda: _next(ids),
            projection_recorder=projection,
        )
        result = port.execute(
            CommandCall(
                command_name="create",
                arguments={"title": "Projected task"},
                owner_id="owner-1",
                principal_class="agent",
                run_id=run_id,
                request_id=request_id,
                now=NOW,
            )
        )
        assert result.ok
        event_id = uuid.UUID(result.data["projection_event_id"])
        claim = projection.claim_next(worker_id="projector", now=NOW, ttl=timedelta(minutes=2))
        attempt = projection.begin_attempt(
            event_id=event_id,
            claim_token=claim.claim_token,
            worker_id="projector",
            request_identity="create-request-1",
            request_payload={"name": "Projected task"},
            intended_external_id=None,
            started_at=NOW,
        )
        correlation = projection.resolve_create_correlation(
            event_id=event_id,
            attempt_id=attempt.attempt_id,
            external_matches=["987654321"],
            observed_at=NOW,
            evidence={"marker_search": "one"},
        )
        assert correlation.state == "bound" and correlation.matched_external_id == "987654321"
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        adjudication = projection.record_observation_and_adjudicate(
            attempt_id=attempt.attempt_id,
            observation_kind="marker_search",
            observed_applied=True,
            observed_identity=event.idempotency_key,
            reread_complete=True,
            evidence={"gid": "987654321"},
            decided_by="automatic",
            decision_reason="exact marker",
            observed_at=NOW,
        )
        assert adjudication.outcome == "confirmed"
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "applied"
        alias = session.scalar(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.task_id == uuid.UUID(result.data["task_id"]),
                models.TaskExternalAlias.origin == "projection",
            )
        )
        assert alias.external_id == "987654321"

        # A distinct create event with multiple marker matches cannot bind or continue.
        second_run, second_request = _next(ids), _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=second_run)
        second = port.execute(
            CommandCall(
                command_name="create",
                arguments={"title": "Ambiguous projected task"},
                owner_id="owner-1",
                principal_class="agent",
                run_id=second_run,
                request_id=second_request,
                now=NOW,
            )
        )
        second_event_id = uuid.UUID(second.data["projection_event_id"])
        second_claim = projection.claim_next(
            worker_id="projector", now=NOW, ttl=timedelta(minutes=2)
        )
        second_attempt = projection.begin_attempt(
            event_id=second_event_id,
            claim_token=second_claim.claim_token,
            worker_id="projector",
            request_identity="create-request-2",
            request_payload={"name": "Ambiguous projected task"},
            intended_external_id=None,
            started_at=NOW,
        )
        blocked = projection.resolve_create_correlation(
            event_id=second_event_id,
            attempt_id=second_attempt.attempt_id,
            external_matches=["111111111", "222222222"],
            observed_at=NOW,
            evidence={"marker_search": "multiple"},
        )
        assert blocked.state == "ambiguous"
        assert session.get(tx.ProjectionOutboxEvent, second_event_id).state == "blocked"
        assert blocked.mapping_id is None


def test_uncertain_projection_can_be_settled_later_by_exact_admin_recovery(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        projection = _projection(session, ids)
        projection.activate_epoch(
            generation_id=context["generation_id"], activation_reason="recovery", created_at=NOW
        )
        execution_id = _claimed_execution(session, ids, context, task_id)
        event_id = projection.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "v3"},
            created_at=NOW,
        )
        claim = projection.claim_next(worker_id="projector", now=NOW, ttl=timedelta(minutes=2))
        attempt = projection.begin_attempt(
            event_id=event_id,
            claim_token=claim.claim_token,
            worker_id="projector",
            request_identity="write-lost-response",
            request_payload={"notes": "v3"},
            intended_external_id="123456789",
            started_at=NOW,
        )
        first = projection.record_observation_and_adjudicate(
            attempt_id=attempt.attempt_id,
            observation_kind="reread",
            observed_applied=None,
            observed_identity=None,
            reread_complete=False,
            evidence={"timeout": True},
            decided_by="automatic",
            decision_reason="reread incomplete",
            observed_at=NOW,
        )
        assert first.outcome == "uncertain"
        assert projection.unresolved_attempt_id(task_id) == attempt.attempt_id

        recovery_run, recovery_request = _next(ids), _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=recovery_run,
            owner="marco",
        )
        port = PostgresCommandPort(
            session,
            cursor_secret=SECRET,
            uuid_factory=lambda: _next(ids),
            projection_recorder=projection,
        )
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        recovered = port.execute(
            CommandCall(
                command_name="recover",
                arguments={
                    "task_id": str(task_id),
                    "attempt_id": str(attempt.attempt_id),
                    "observed_applied": True,
                    "observed_identity": event.idempotency_key,
                    "reread_complete": True,
                    "evidence": {"manual_reread": "exact"},
                },
                owner_id="marco",
                principal_class="admin",
                run_id=recovery_run,
                request_id=recovery_request,
                now=NOW,
            )
        )
        assert recovered.ok and recovered.data["outcome"] == "confirmed"
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "applied"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionObservation).where(
                tx.ProjectionObservation.attempt_id == attempt.attempt_id
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionAdjudication).where(
                tx.ProjectionAdjudication.attempt_id == attempt.attempt_id
            )
        ) == 2


def test_retired_epoch_fences_stale_workers_and_drift_reprojects_authority(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        projection = _projection(session, ids)
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"], activation_reason="epoch one", created_at=NOW
        )
        projection.bind_imported_mappings(generation_id=context["generation_id"], bound_at=NOW)
        mapping = session.scalar(
            select(tx.TaskProjectionMapping).where(tx.TaskProjectionMapping.task_id == task_id)
        )
        drift = projection.record_drift_and_reproject(
            generation_id=context["generation_id"],
            task_id=task_id,
            task_mapping_id=mapping.mapping_id,
            drift_kind="document",
            external_snapshot={"notes": "edited outside Dish"},
            authoritative_snapshot={"notes": "canonical"},
            evidence={"scan": "reconciler-1"},
            detected_at=NOW,
        )
        assert drift.state == "reprojected"
        assert session.get(tx.ProjectionOutboxEvent, drift.reproject_event_id).source_route == "service"
        projection.retire_epoch(projection_epoch_id=epoch.projection_epoch_id, retired_at=NOW)
        assert session.get(tx.ProjectionOutboxEvent, drift.reproject_event_id).state == "superseded"
        assert session.get(tx.TaskProjectionMapping, mapping.mapping_id).state == "retired"
        assert projection.claim_next(worker_id="stale-worker", now=NOW, ttl=timedelta(minutes=2)) is None


def test_reconciliation_blocks_unknown_external_objects(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        projection = _projection(session, ids)
        projection.activate_epoch(
            generation_id=context["generation_id"], activation_reason="corpus", created_at=NOW
        )
        run = projection.start_reconciliation(
            generation_id=context["generation_id"],
            corpus_identity="asana-project-snapshot-1",
            expected_items=2,
            started_at=NOW,
        )
        projection.record_reconciliation_item(
            reconciliation_run_id=run.reconciliation_run_id,
            item_identity="task:123456789",
            entity_kind="task",
            mapping_id=None,
            outcome="matched",
            evidence={"gid": "123456789"},
            recorded_at=NOW,
        )
        with pytest.raises(TransitionAuthorityError, match="incomplete"):
            projection.complete_reconciliation(
                reconciliation_run_id=run.reconciliation_run_id, completed_at=NOW
            )
        projection.record_reconciliation_item(
            reconciliation_run_id=run.reconciliation_run_id,
            item_identity="task:999999999",
            entity_kind="task",
            mapping_id=None,
            outcome="unknown_external",
            evidence={"gid": "999999999"},
            recorded_at=NOW,
        )
        assert projection.complete_reconciliation(
            reconciliation_run_id=run.reconciliation_run_id, completed_at=NOW
        ).status == "blocked"


def test_authoritative_create_and_projection_outbox_roll_back_together(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        projection = _projection(session, ids)
        projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="atomic rollback",
            created_at=NOW,
        )

    with pytest.raises(RuntimeError, match="force rollback"):
        with session_scope(factory) as session:
            run_id, request_id = _next(ids), _next(ids)
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            projection = _projection(session, ids)
            port = PostgresCommandPort(
                session,
                cursor_secret=SECRET,
                uuid_factory=lambda: _next(ids),
                projection_recorder=projection,
            )
            result = port.execute(
                CommandCall(
                    command_name="create",
                    arguments={"title": "Rolled back task"},
                    owner_id="owner-1",
                    principal_class="agent",
                    run_id=run_id,
                    request_id=request_id,
                    now=NOW,
                )
            )
            assert result.ok
            assert session.get(
                tx.ProjectionOutboxEvent, uuid.UUID(result.data["projection_event_id"])
            ) is not None
            raise RuntimeError("force rollback")

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(models.DishTask).where(
                models.DishTask.creation_route == "create"
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent)
        ) == 0


def test_projection_mapping_cannot_transfer_an_alias_between_tasks(workflow_db) -> None:
    factory, ids, context, first_task_id = workflow_db
    with pytest.raises(IntegrityError, match="projection mapping identity mismatch"):
        with session_scope(factory) as session:
            second = _import_one(session, ids, context, asana_gid="123456790")
            projection = _projection(session, ids)
            first_epoch = projection.activate_epoch(
                generation_id=context["generation_id"],
                activation_reason="mapping identity",
                created_at=NOW,
            )
            projection.bind_imported_mappings(
                generation_id=context["generation_id"], bound_at=NOW
            )
            first_alias = session.scalar(
                select(models.TaskExternalAlias).where(
                    models.TaskExternalAlias.task_id == first_task_id
                )
            )
            projection.retire_epoch(
                projection_epoch_id=first_epoch.projection_epoch_id, retired_at=NOW
            )
            second_epoch = projection.activate_epoch(
                generation_id=context["generation_id"],
                activation_reason="mapping identity retry",
                created_at=NOW,
            )
            session.add(
                tx.TaskProjectionMapping(
                    mapping_id=_next(ids),
                    generation_id=context["generation_id"],
                    projection_epoch_id=second_epoch.projection_epoch_id,
                    task_id=second.task_id,
                    alias_id=first_alias.alias_id,
                    state="active",
                    mapping_revision=1,
                    bound_at=NOW,
                    retired_at=None,
                )
            )
            session.flush()
