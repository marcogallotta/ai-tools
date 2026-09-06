from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.workflow import WorkflowAuthorityService
from tests.support.postgresql.workflow import (
    _admit,
    _execution,
    _next,
    _register_run,
    workflow_db,
)


NOW = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("command_name", "binds_prelock_observation"),
    (("start", True), ("record-cook-log", False)),
)
def test_task_fence_capture_preserves_mutation_observation_but_cook_log_rebinds(
    workflow_db, monkeypatch, command_name: str, binds_prelock_observation: bool
) -> None:
    factory, ids, context, task_id = workflow_db
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
            command=command_name,
        )
        _execution(
            service,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
            command=command_name,
        )
        observed_state = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        observed_membership = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        assert observed_state is not None and observed_membership is not None
        observed_dish_version = observed_state.dish_version
        observed_membership_revision = observed_membership.membership_revision

        def refresh_like_concurrent_winner(*, generation_id, task_id):
            from types import SimpleNamespace

            assert generation_id == context["generation_id"]
            assert task_id == observed_state.task_id
            locked_state = SimpleNamespace(
                dish_version=observed_dish_version + 1,
                placement_version=observed_state.placement_version,
                catalog_version_id=observed_state.catalog_version_id,
                archived_at=observed_state.archived_at,
            )
            locked_membership = SimpleNamespace(
                membership_revision=observed_membership_revision + 1
            )
            return locked_state, locked_membership, None

        monkeypatch.setattr(
            service.repo, "lock_task_currentness", refresh_like_concurrent_winner
        )
        fence = service.repo.capture_task_fence(
            execution_id=execution_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )

        if binds_prelock_observation:
            assert fence.expected_dish_version == observed_dish_version
            assert fence.expected_membership_revision == observed_membership_revision
        else:
            assert fence.expected_dish_version == observed_dish_version + 1
            assert fence.expected_membership_revision == observed_membership_revision + 1
