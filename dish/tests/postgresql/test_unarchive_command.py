from __future__ import annotations

from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import ACTION_COMMANDS, definition_for
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from dish_pg.workflow import UNARCHIVE_FRESH_RUN_GUIDANCE
from tests.support.postgresql.command import _call, _port, _start_initial
from tests.support.postgresql.workflow import _next, _register_run, workflow_db


def _archive_receipts(session, context, task_id) -> int:
    return session.scalar(
        select(func.count()).select_from(models.DishMutationReceipt).where(
            models.DishMutationReceipt.generation_id == context["generation_id"],
            models.DishMutationReceipt.task_id == task_id,
            models.DishMutationReceipt.archive_changed.is_(True),
        )
    )


def _agent_archive(port, ids, *, task_id, run_id):
    result = port.execute(
        _call("archive", run_id=run_id, request_id=_next(ids), arguments={"task_id": str(task_id)})
    )
    assert result.ok, result
    return result


def test_unarchive_is_agent_and_admin_reachable_but_not_yet_action_exposed() -> None:
    definition = definition_for("unarchive")
    assert definition.principal == "agent"
    assert definition.admin_exposed is True
    assert definition.action_exposed is False
    assert "unarchive" not in ACTION_COMMANDS


def test_agent_unarchive_restores_active_dish_in_its_section_and_requires_fresh_run(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    old_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=old_run)
        port = _port(session, ids)
        before = session.get(models.DishState, (context["generation_id"], task_id))
        section_id = before.section_id
        completion = (before.completed, before.completion_reason, before.completion_version)
        _agent_archive(port, ids, task_id=task_id, run_id=old_run)

        # The archiving run may undo its own archive; unarchive only clears the flag.
        unarchived = port.execute(
            _call("unarchive", run_id=old_run, request_id=_next(ids), arguments={"task_id": str(task_id)})
        )

        assert unarchived.ok, unarchived
        assert unarchived.data["completion_state"] == "active"
        assert unarchived.data["fresh_run_required"] is True
        assert unarchived.data["agent_guidance"] == UNARCHIVE_FRESH_RUN_GUIDANCE
        assert "system_reason" not in unarchived.data
        current = session.get(models.DishState, (context["generation_id"], task_id))
        session.refresh(current)
        assert current.archived_at is None
        assert current.section_id == section_id
        assert (current.completed, current.completion_reason, current.completion_version) == completion
        receipt = session.get(
            models.DishMutationReceipt, (context["generation_id"], task_id, current.dish_version)
        )
        assert receipt.archive_changed is True
        assert not (receipt.completion_changed or receipt.placement_changed or receipt.content_changed)
        assert _archive_receipts(session, context, task_id) == 2
        view = PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)
        assert view.completion_state == "active"
        page = PostgresReadModel(session, cursor_secret=b"r" * 32).section_tasks(
            section_reference=context["section_id"]
        )
        assert task_id in {item.task_id for item in page.items}

        stale = port.execute(
            _call(
                "start",
                run_id=old_run,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert stale.ok is False
        assert stale.code == "AUTHORITY_MISMATCH"
        assert UNARCHIVE_FRESH_RUN_GUIDANCE in str(stale.data)

        fresh_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=fresh_run)
        _start_initial(port, ids, task_id=task_id, run_id=fresh_run)


def test_unarchive_rejects_unarchived_dish_without_effect(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        version = session.get(models.DishState, (context["generation_id"], task_id)).dish_version

        result = port.execute(
            _call("unarchive", run_id=run_id, request_id=_next(ids), arguments={"task_id": str(task_id)})
        )

        assert result.ok is False
        assert result.code == "TASK_NOT_ARCHIVED"
        assert session.get(models.DishState, (context["generation_id"], task_id)).dish_version == version
        assert _archive_receipts(session, context, task_id) == 0


def test_unarchive_replay_is_effect_free_and_rearchive_revokes_again(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    first_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=first_run)
        port = _port(session, ids)
        _agent_archive(port, ids, task_id=task_id, run_id=first_run)
        second_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=second_run)
        request_id = _next(ids)
        call = _call("unarchive", run_id=second_run, request_id=request_id, arguments={"task_id": str(task_id)})
        unarchived = port.execute(call)
        assert unarchived.ok, unarchived

        replay = port.execute(call)

        assert replay.ok is True
        assert replay.request_replayed is True
        assert replay.data == unarchived.data
        assert _archive_receipts(session, context, task_id) == 2

        _agent_archive(port, ids, task_id=task_id, run_id=second_run)
        assert _archive_receipts(session, context, task_id) == 3
        assert session.scalar(
            select(func.count()).select_from(wf.TaskRunRevocation).where(
                wf.TaskRunRevocation.generation_id == context["generation_id"],
                wf.TaskRunRevocation.task_id == task_id,
                wf.TaskRunRevocation.run_id == second_run,
            )
        ) == 1
        blocked = port.execute(
            _call(
                "start",
                run_id=second_run,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
            )
        )
        assert blocked.code == "TASK_ARCHIVED"


def test_admin_unarchive_needs_no_confirmation_and_records_admin_reason(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    admin_run = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=admin_run, owner="Marco")
        port = _port(session, ids)
        archived = port.execute(
            _call(
                "archive",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "confirmed": True},
            )
        )
        assert archived.ok, archived

        unarchived = port.execute(
            _call(
                "unarchive",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id)},
            )
        )

        assert unarchived.ok, unarchived
        assert unarchived.data["system_reason"] == "admin_unarchive"
        assert unarchived.data["authority_mode"] == "postgresql"
        assert unarchived.data["completion_state"] == "active"
