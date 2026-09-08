"""Shared PR3-staging fixtures for native-section content materializer tests.

Extracted from tests/postgresql/test_native_section_content_materializer.py so
other test modules can reuse them without importing another collected test
module directly (see tests/test_test_module_import_contract.py).
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg.native_section_carry_forward import (
    apply_carry_forward,
    build_carry_forward_plan,
)
from dish_pg.workflow import WorkflowAuthorityService
from tests.support.postgresql.core import _next
from tests.support.postgresql.native_section_content_fixtures import (
    NOW,
    SOURCE_COMMIT,
    _fixture,
)
from tests.support.postgresql.workflow import _admit, _execution, _register_run


def _stage_pr3(session: Session, ids: Iterator[uuid.UUID], **fixture_kwargs):
    seeded, expectation, source_rows = _fixture(session, ids, **fixture_kwargs)
    plan = build_carry_forward_plan(session, expectation=expectation)
    receipt = apply_carry_forward(
        session,
        expected_snapshot_sha256=plan.source_snapshot_sha256,
        source_commit=SOURCE_COMMIT,
        expectation=expectation,
        now=NOW,
    )
    occurrences = tuple(
        session.scalars(
            select(models.NativeSectionContentCarryForwardOccurrence).order_by(
                models.NativeSectionContentCarryForwardOccurrence.task_id
            )
        )
    )
    return (
        seeded,
        expectation,
        source_rows,
        uuid.UUID(receipt["migration_event_id"]),
        occurrences,
    )


def _complete_after_staging(
    session: Session, ids: Iterator[uuid.UUID], seeded, task_id: uuid.UUID
) -> int:
    state = session.get(models.DishState, (seeded["generation_id"], task_id))
    assert state is not None
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    _register_run(session, generation_id=seeded["generation_id"], run_id=run_id)
    workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
    _admit(
        workflow,
        request_id=request_id,
        generation_id=seeded["generation_id"],
        run_id=run_id,
        command="cooked",
        payload={"dish_id": str(task_id)},
    )
    _execution(
        workflow,
        execution_id=execution_id,
        request_id=request_id,
        generation_id=seeded["generation_id"],
        task_id=task_id,
        binding_id=seeded["binding_id"],
        command="cooked",
    )
    next_version = state.dish_version + 1
    session.add(
        models.DishMutationReceipt(
            generation_id=seeded["generation_id"],
            task_id=task_id,
            dish_version=next_version,
            source_route="command_execution",
            import_run_id=None,
            command_execution_id=execution_id,
            content_changed=False,
            placement_changed=False,
            completion_changed=True,
            archive_changed=False,
            occurred_at=NOW + timedelta(minutes=1),
        )
    )
    session.flush()
    changed = session.execute(
        update(models.DishState)
        .where(
            models.DishState.generation_id == seeded["generation_id"],
            models.DishState.task_id == task_id,
            models.DishState.dish_version == state.dish_version,
        )
        .values(
            completed=True,
            completion_reason="cooked",
            dish_version=next_version,
            completion_version=next_version,
            updated_at=NOW + timedelta(minutes=1),
        )
        .execution_options(synchronize_session=False)
    )
    assert changed.rowcount == 1
    session.flush()
    return next_version
