from __future__ import annotations

from datetime import timedelta

from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_shadow.policy import DARK_LAUNCH_TREATMENTS, treatment_for
from tests.support.postgresql.workflow import NOW, _claimed_execution, workflow_db


def test_dark_launch_registry_covers_current_command_surfaces() -> None:
    from dish_pg.command_contract import COMMAND_DEFINITIONS
    from dish_service.command_spec import ACTION_COMMANDS

    assert set(COMMAND_DEFINITIONS) == set(DARK_LAUNCH_TREATMENTS)
    assert set(ACTION_COMMANDS).issubset(DARK_LAUNCH_TREATMENTS)
    assert treatment_for("create").treatment == "capture_only"
    assert treatment_for("prepare").treatment == "execute"
    assert treatment_for("recover").treatment == "capture_only"
    assert all(not row.external_effects_allowed for row in DARK_LAUNCH_TREATMENTS.values())


def test_projection_epoch_is_fail_closed_until_effects_are_explicitly_enabled(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=lambda: next(ids))
        epoch = service.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="dark launch",
            created_at=NOW,
        )
        execution_id = _claimed_execution(session, ids, context, task_id)
        event_id = service.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "shadow-v2"},
            created_at=NOW,
        )
        assert epoch.external_effects_enabled is False
        assert service.claim_next(
            worker_id="projector", now=NOW, ttl=timedelta(minutes=2)
        ) is None

        service.set_external_effects_enabled(
            projection_epoch_id=epoch.projection_epoch_id,
            enabled=True,
            reason="explicit post-dark-launch projection authorization",
        )
        claim = service.claim_next(
            worker_id="projector", now=NOW, ttl=timedelta(minutes=2)
        )
        assert claim is not None
        assert claim.event_id == event_id
