from __future__ import annotations

from datetime import timedelta

from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_shadow.policy import DARK_LAUNCH_TREATMENTS, treatment_for
from tests.support.postgresql.workflow import NOW, _claimed_execution, workflow_db


def test_every_current_action_and_admin_command_has_a_treatment() -> None:
    from dish_service.command_spec import ACTION_COMMAND_DEFINITIONS
    from dish_tool.admin_command_spec import ADMIN_COMMAND_SPECS

    current_surface_commands = set(ACTION_COMMAND_DEFINITIONS) | set(ADMIN_COMMAND_SPECS)

    for command_name in current_surface_commands:
        assert treatment_for(command_name).command_name == command_name


def test_shadow_only_exceptions_keep_their_intended_treatments() -> None:
    import dish_shadow.policy as shadow_policy

    assert len(shadow_policy._SHADOW_ONLY_OVERRIDES) == 12
    assert treatment_for("create").treatment == "execute"
    assert treatment_for("recover").treatment == "capture_only"
    assert treatment_for("repair-destination").treatment == "capture_only"
    assert treatment_for("proposals").treatment == "excluded"
    assert treatment_for("apply-proposal").treatment == "capture_only"
    assert treatment_for("safe-reclaim").treatment == "capture_only"
    assert treatment_for("review-queue").treatment == "excluded"
    assert treatment_for("review-inspect").treatment == "excluded"
    assert treatment_for("review-approve").treatment == "capture_only"
    assert treatment_for("review-reject").treatment == "capture_only"
    assert treatment_for("kill").treatment == "capture_only"


def test_operator_queue_commands_are_explicitly_shadow_excluded() -> None:
    import dish_shadow.policy as shadow_policy

    assert shadow_policy._SHADOW_ONLY_OVERRIDES["active"] == (
        "excluded",
        "read-only operator active-work query",
    )
    assert shadow_policy._SHADOW_ONLY_OVERRIDES["queue"] == (
        "excluded",
        "read-only operator queue",
    )


def test_retained_target_mutations_derive_execute() -> None:
    import dish_shadow.policy as shadow_policy
    from dish_pg.command_contract import COMMAND_DEFINITIONS

    for command_name, definition in COMMAND_DEFINITIONS.items():
        if command_name in shadow_policy._SHADOW_ONLY_OVERRIDES:
            continue
        if definition.retained and definition.profile != "Q":
            assert treatment_for(command_name).treatment == "execute"


def test_target_queries_and_retired_commands_derive_excluded() -> None:
    import dish_shadow.policy as shadow_policy
    from dish_pg.command_contract import COMMAND_DEFINITIONS

    for command_name, definition in COMMAND_DEFINITIONS.items():
        if command_name in shadow_policy._SHADOW_ONLY_OVERRIDES:
            continue
        if not definition.retained or definition.profile == "Q":
            assert treatment_for(command_name).treatment == "excluded"


def test_planning_intent_settlement_remains_intentionally_target_only() -> None:
    from dish_pg.command_contract import COMMAND_DEFINITIONS
    from dish_service.command_spec import ACTION_COMMAND_DEFINITIONS
    from dish_tool.admin_command_spec import ADMIN_COMMAND_SPECS

    current_surface_commands = set(ACTION_COMMAND_DEFINITIONS) | set(ADMIN_COMMAND_SPECS)

    assert "planning-intent-settlement" in COMMAND_DEFINITIONS
    assert "planning-intent-settlement" not in current_surface_commands
    assert treatment_for("planning-intent-settlement").treatment == "execute"


def test_comparison_eligibility_derives_from_treatment() -> None:
    for row in DARK_LAUNCH_TREATMENTS.values():
        assert row.comparison_eligible is (row.treatment == "execute")
        assert row.external_effects_allowed is False



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


def test_shadow_origin_is_rejected_even_when_projection_effects_are_enabled(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=lambda: next(ids))
        service.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="live projection enabled",
            external_effects_enabled=True,
            created_at=NOW,
        )
        execution_id = _claimed_execution(session, ids, context, task_id)
        service.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "shadow-v2"},
            origin="shadow",
            created_at=NOW,
        )

        assert service.claim_next(
            worker_id="projector", now=NOW, ttl=timedelta(minutes=2)
        ) is None
