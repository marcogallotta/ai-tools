from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from tests.support.postgresql.core import _import_one
from tests.support.postgresql.workflow import (
    _claimed_execution,
    NOW, _admit, _execution, _next, _register_run, workflow_db,
)

SECRET = b"stage-5-cursor-secret-32-bytes!!"


def _projection(session, ids) -> ProjectionService:
    return ProjectionService(session, uuid_factory=lambda: _next(ids))


def _uncertain_projection_admin_recovery_scenario(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        projection = _projection(session, ids)
        projection.activate_epoch(
            generation_id=context["generation_id"], activation_reason="recovery", created_at=NOW,
            external_effects_enabled=True,
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
            claim_revision=claim.claim_revision,
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
            evidence={"external_observation": {"source": "external_reread", "operation": "update_task_document", "observed_external_id": "123456789", "available": False}},
            decided_by="automatic",
            decision_reason="reread incomplete",
            observed_at=NOW,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="projector",
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
                    "observed_identity": attempt.request_sha256,
                    "reread_complete": True,
                    "evidence": {
                        "external_observation": {
                            "source": "external_reread",
                            "operation": "update_task_document",
                            "observed_external_id": "123456789",
                            "observed_document_identity": attempt.request_sha256,
                        }
                    },
                },
                owner_id="marco",
                principal_class="admin",
                run_id=recovery_run,
                request_id=recovery_request,
                now=NOW,
            )
        )
        assert recovered.ok and recovered.data["outcome"] == "confirmed"
        recovery_attempt_id = uuid.UUID(recovered.data["attempt_id"])
        assert recovery_attempt_id != attempt.attempt_id
        assert recovered.data["predecessor_attempt_id"] == str(attempt.attempt_id)
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "applied"
        original = session.get(tx.ProjectionAttempt, attempt.attempt_id)
        recovery = session.get(tx.ProjectionAttempt, recovery_attempt_id)
        assert original.state == "uncertain" and original.terminal_at == NOW
        assert recovery.state == "confirmed" and recovery.predecessor_attempt_id == original.attempt_id
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionObservation).where(
                tx.ProjectionObservation.attempt_id == attempt.attempt_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionAdjudication).where(
                tx.ProjectionAdjudication.attempt_id == attempt.attempt_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionObservation).where(
                tx.ProjectionObservation.attempt_id == recovery_attempt_id
            )
        ) == 1


def test_uncertain_projection_can_be_settled_later_by_exact_admin_recovery(
    workflow_db,
) -> None:
    _uncertain_projection_admin_recovery_scenario(workflow_db)


def test_retired_epoch_fences_stale_workers_and_drift_reprojects_authority(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        projection = _projection(session, ids)
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"], activation_reason="epoch one", created_at=NOW,
            external_effects_enabled=True,
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
            generation_id=context["generation_id"], activation_reason="corpus", created_at=NOW,
            external_effects_enabled=True,
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
            external_effects_enabled=True,
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
                external_effects_enabled=True,
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
                external_effects_enabled=True,
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
