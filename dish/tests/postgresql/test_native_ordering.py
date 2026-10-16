from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.read_model import ReadModelError
from tests.postgresql.test_native_read_authority import (
    _add_native_task,
    _install_native_read_authority,
)
from tests.support.postgresql.command import _call, _port
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db


def _native_fixture(session, ids, context, *, task_count: int = 3):
    second_section_id = _next(ids)
    native = _install_native_read_authority(
        session,
        ids,
        context,
        section_ids=(context["section_id"], second_section_id),
    )
    task_ids = tuple(
        _add_native_task(
            session,
            ids,
            context,
            section_id=second_section_id,
            catalog_version_id=native["catalog_version_id"],
            title=f"Native task {index}",
        )
        for index in range(task_count)
    )
    for task_id in task_ids:
        session.add(
            models.TaskMembershipHead(
                generation_id=context["generation_id"],
                task_id=task_id,
                membership_revision=0,
                updated_at=NOW,
            )
        )
    session.flush()
    return second_section_id, native, task_ids


def test_within_section_reorder_is_rank_only_and_stales_old_cursor(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        section_id, native, task_ids = _native_fixture(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)

        first_page = port.reads.section_tasks(section_reference=section_id, page_size=2)
        assert [item.task_id for item in first_page.items] == list(task_ids[:2])
        assert first_page.read_authority["section_order_version"] == 4
        assert first_page.next_cursor is not None

        before_state = session.get(
            models.DishState, (context["generation_id"], task_ids[2])
        )
        assert before_state is not None
        before_identity = (
            before_state.current_content_version_id,
            before_state.section_id,
            before_state.dish_version,
            before_state.placement_version,
            before_state.completion_version,
        )
        call = _call(
            "reorder-dish",
            run_id=run_id,
            request_id=request_id,
            arguments={
                "dish_id": str(task_ids[2]),
                "section_id": str(section_id),
                "position": {"edge": "start"},
                "expected_order_version": 4,
                "agent": "gpt",
            },
        )
        first = port.execute(call)
        replay = port.execute(call)

        assert first.ok is True
        assert first.data["section_order_version"] == 5
        assert replay.ok is True
        assert replay.request_replayed is True
        assert replay.data == first.data
        after_state = session.get(
            models.DishState, (context["generation_id"], task_ids[2])
        )
        assert after_state is not None
        assert (
            after_state.current_content_version_id,
            after_state.section_id,
            after_state.dish_version,
            after_state.placement_version,
            after_state.completion_version,
        ) == before_identity

        reordered = port.reads.section_tasks(section_reference=section_id, page_size=10)
        assert [item.task_id for item in reordered.items] == [
            task_ids[2],
            task_ids[0],
            task_ids[1],
        ]
        assert reordered.read_authority["section_order_version"] == 5
        with pytest.raises(ReadModelError, match="cursor is stale or belongs to another list query"):
            port.reads.section_tasks(
                section_reference=section_id,
                page_size=2,
                cursor=first_page.next_cursor,
            )


def test_section_reorder_advances_native_catalog_runtime_without_moving_dishes(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        second_section_id, native, task_ids = _native_fixture(
            session, ids, context, task_count=1
        )
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        tracked_task_ids = [context_task_id for context_task_id in task_ids]
        before_states = {
            task_id: session.get(models.DishState, (context["generation_id"], task_id))
            for task_id in tracked_task_ids
        }
        before_identity = {
            task_id: (
                state.section_id,
                state.dish_version,
                state.placement_version,
                state.completion_version,
            )
            for task_id, state in before_states.items()
            if state is not None
        }
        port = _port(session, ids)
        call = _call(
            "reorder-sections",
            run_id=run_id,
            request_id=request_id,
            arguments={
                "ordered_section_ids": [
                    str(second_section_id),
                    str(context["section_id"]),
                ],
                "expected_catalog_version_id": str(native["catalog_version_id"]),
                "expected_runtime_attestation_id": str(native["attestation_id"]),
                "agent": "gpt",
            },
        )

        first = port.execute(call)
        replay = port.execute(call)
        assert first.ok is True
        assert replay.ok is True
        assert replay.request_replayed is True
        assert replay.data == first.data
        assert first.data["ordered_section_ids"] == [
            str(second_section_id),
            str(context["section_id"]),
        ]
        assert first.data["catalog_version_id"] != str(native["catalog_version_id"])
        assert first.data["runtime_attestation_id"] != str(native["attestation_id"])
        assert first.data["catalog_revision"] == 2
        assert first.data["runtime_attestation_revision"] == 2

        sections = port.reads.sections()
        assert [row["section_id"] for row in sections] == [
            str(second_section_id),
            str(context["section_id"]),
        ]
        for task_id, prior in before_identity.items():
            state = session.get(models.DishState, (context["generation_id"], task_id))
            assert state is not None
            assert state.catalog_version_id == native["catalog_version_id"]
            assert (
                state.section_id,
                state.dish_version,
                state.placement_version,
                state.completion_version,
            ) == prior
        assert session.scalar(
            select(func.count())
            .select_from(models.NativeCatalogRuntimeAttestation)
            .where(models.NativeCatalogRuntimeAttestation.generation_id == context["generation_id"])
        ) == 2

        # The Dish keeps its original placement-catalog witness. A later
        # order-only catalog successor remains compatible for task commands.
        current_head = session.get(
            models.SectionDishOrderHead,
            (context["generation_id"], second_section_id),
        )
        assert current_head is not None
        compatible_reorder = port.execute(
            _call(
                "reorder-dish",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "dish_id": str(task_ids[0]),
                    "section_id": str(second_section_id),
                    "position": {"edge": "start"},
                    "expected_order_version": current_head.order_version,
                    "agent": "gpt",
                },
            )
        )
        assert compatible_reorder.ok is True, compatible_reorder.data

        stale = port.execute(
            _call(
                "reorder-sections",
                run_id=run_id,
                request_id=_next(ids),
                arguments={
                    "ordered_section_ids": [
                        str(context["section_id"]),
                        str(second_section_id),
                    ],
                    "expected_catalog_version_id": str(native["catalog_version_id"]),
                    "expected_runtime_attestation_id": str(native["attestation_id"]),
                    "agent": "gpt",
                },
            )
        )
        assert stale.ok is False
        assert stale.code == "ORDER_AUTHORITY_STALE"
