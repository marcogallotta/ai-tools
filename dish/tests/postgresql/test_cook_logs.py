from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import uuid

import pytest
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import (
    postgres_action_argument_schema,
    validate_postgres_action_request,
)
from dish_pg.database import session_scope
from dish_pg.frontend_board_query import FrontendBoardQuery
from dish_service.cli import build_parser
from sqlalchemy import func, select

from tests.support.postgresql.command import _call, _port, _start_initial
from tests.support.postgresql.core import _import_one
from tests.support.postgresql.workflow import NOW, _next, _register_run


def test_cook_log_action_contract_preserves_text_and_bounds_reads() -> None:
    run_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    dish_id = str(uuid.uuid4())
    schema = postgres_action_argument_schema("record-cook-log")
    assert schema["required"] == ["dish_id", "agent", "text"]
    assert "request_id" not in schema["properties"]
    client, arguments = validate_postgres_action_request(
        "record-cook-log",
        {"client": {"run_id": run_id, "request_id": request_id}, "arguments": {"dish_id": dish_id, "agent": "gpt", "text": "  observed  "}},
    )
    assert client == {"run_id": run_id, "request_id": request_id}
    assert arguments == {"dish_id": dish_id, "agent": "gpt", "text": "  observed  "}
    client, arguments = validate_postgres_action_request(
        "cook-logs",
        {"client": {"run_id": run_id}, "arguments": {"dish_id": dish_id, "agent": "gpt", "page_size": 1}},
    )
    assert client == {"run_id": run_id}
    assert arguments["page_size"] == 1

    parsed = build_parser().parse_args(["record-cook-log", dish_id, "--agent", "gpt", "--text", "note", "--request-id", request_id])
    assert parsed.command == "record-cook-log"
    assert parsed.dish_id == dish_id
    parsed = build_parser().parse_args(["cook-logs", dish_id, "--agent", "gpt", "--page-size", "1"])
    assert parsed.command == "cook-logs"
    assert parsed.page_size == 1


def test_cooked_updates_contract_cli_and_incremental_window() -> None:
    run_id = str(uuid.uuid4())
    generation_id = str(uuid.uuid4())
    schema = postgres_action_argument_schema("cooked-updates")
    assert schema["required"] == ["since", "agent"]
    assert "request_id" not in schema["properties"]
    client, arguments = validate_postgres_action_request(
        "cooked-updates",
        {
            "client": {"run_id": run_id},
            "arguments": {
                "since": "2026-08-01T20:00:01Z",
                "agent": "gpt",
                "generation_id": generation_id,
                "page_size": 1,
            },
        },
    )
    assert client == {"run_id": run_id}
    assert arguments == {
        "since": "2026-08-01T20:00:01+00:00",
        "agent": "gpt",
        "generation_id": generation_id,
        "page_size": 1,
    }
    parsed = build_parser().parse_args(
        [
            "cooked-updates",
            "2026-08-01T20:00:01Z",
            "--agent",
            "gpt",
            "--generation-id",
            generation_id,
            "--page-size",
            "1",
        ]
    )
    assert parsed.command == "cooked-updates"
    assert parsed.generation_id == generation_id
    assert parsed.page_size == 1


def test_cooked_updates_lists_current_cooked_state_by_fixed_event_window_and_keyset(
    workflow_db,
) -> None:
    factory, ids, context, first_task_id = workflow_db
    second_task_id = _next(ids)
    with session_scope(factory) as session:
        _import_one(
            session,
            ids,
            context,
            task_id=second_task_id,
            asana_gid="987654321",
        )
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)

        assert port.execute(
            replace(
                _call(
                    "cooked",
                    run_id=run_id,
                    request_id=_next(ids),
                    arguments={"dish_id": str(first_task_id), "agent": "codex"},
                ),
                now=NOW,
            )
        ).ok
        assert port.execute(
            replace(
                _call(
                    "record-cook-log",
                    run_id=run_id,
                    request_id=_next(ids),
                    arguments={
                        "dish_id": str(first_task_id),
                        "agent": "codex",
                        "text": "later evidence",
                    },
                ),
                now=NOW + timedelta(seconds=2),
            )
        ).ok
        assert port.execute(
            replace(
                _call(
                    "cooked",
                    run_id=run_id,
                    request_id=_next(ids),
                    arguments={"dish_id": str(second_task_id), "agent": "codex"},
                ),
                now=NOW + timedelta(seconds=3),
            )
        ).ok

        since = (NOW + timedelta(seconds=1)).isoformat()
        first = port.execute(
            _call(
                "cooked-updates",
                run_id=run_id,
                principal="reader",
                arguments={"since": since, "agent": "codex", "page_size": 1},
            )
        )
        assert first.ok, (first.code, first.data)
        assert first.data["generation_id"] == str(context["generation_id"])
        assert first.data["since"] == since
        assert len(first.data["updates"]) == 1
        assert first.data["updates"][0]["dish_id"] == str(first_task_id)
        assert first.data["updates"][0]["current_cooked_at"] == NOW.isoformat()
        assert first.data["updates"][0]["latest_qualifying_update_at"] == (
            NOW + timedelta(seconds=2)
        ).isoformat()
        assert first.data["next_cursor"] is not None

        second = port.execute(
            _call(
                "cooked-updates",
                run_id=run_id,
                principal="reader",
                arguments={
                    "since": since,
                    "agent": "codex",
                    "page_size": 1,
                    "generation_id": first.data["generation_id"],
                    "cursor": first.data["next_cursor"],
                },
            )
        )
        assert second.ok, (second.code, second.data)
        assert [row["dish_id"] for row in second.data["updates"]] == [str(second_task_id)]
        assert second.data["through"] == first.data["through"]
        assert second.data["next_cursor"] is None


def test_cooked_updates_excludes_currently_uncooked_state_even_with_window_log(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        logged = port.execute(
            replace(
                _call(
                    "record-cook-log",
                    run_id=run_id,
                    request_id=_next(ids),
                    arguments={
                        "dish_id": str(task_id),
                        "agent": "codex",
                        "text": "open-state evidence",
                    },
                ),
                now=NOW + timedelta(seconds=1),
            )
        )
        assert logged.ok, (logged.code, logged.data)

        page = port.execute(
            _call(
                "cooked-updates",
                run_id=run_id,
                principal="reader",
                arguments={
                    "since": NOW.isoformat(),
                    "agent": "codex",
                    "page_size": 10,
                },
            )
        )
        assert page.ok, (page.code, page.data)
        assert page.data["updates"] == []
        assert page.data["next_cursor"] is None


def test_cooked_updates_generation_change_fails_closed_without_advancing_window(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        assert port.execute(
            _call(
                "cooked",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"dish_id": str(task_id), "agent": "codex"},
            )
        ).ok
        page = port.execute(
            _call(
                "cooked-updates",
                run_id=run_id,
                principal="reader",
                arguments={
                    "since": (NOW - timedelta(seconds=1)).isoformat(),
                    "agent": "codex",
                    "page_size": 1,
                },
            )
        )
        assert page.ok
        cursor = port.reads.cursor_codec.encode(
            {
                "kind": "cooked_updates_v1",
                "generation_id": page.data["generation_id"],
                "since": page.data["since"],
                "through": page.data["through"],
                "page_size": 1,
                "after_update_at": page.data["updates"][0]["latest_qualifying_update_at"],
                "after_dish_id": page.data["updates"][0]["dish_id"],
            }
        )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        generation.status = "retired"
        generation.retired_at = NOW + timedelta(days=1)
        session.flush()

        stale = port.execute(
            _call(
                "cooked-updates",
                run_id=run_id,
                principal="reader",
                arguments={
                    "since": page.data["since"],
                    "agent": "codex",
                    "page_size": 1,
                    "generation_id": page.data["generation_id"],
                    "cursor": cursor,
                },
            )
        )
        assert not stale.ok
        assert stale.code == "GENERATION_CHANGED"


def test_record_cook_log_is_version_bound_replay_safe_and_paginated(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        state = session.get(models.DishState, (context["generation_id"], task_id))
        before = (state.current_content_version_id, state.dish_version, state.completed, state.archived_at)
        request_id = _next(ids)

        first = port.execute(_call(
            "record-cook-log", run_id=run_id, request_id=request_id,
            arguments={"dish_id": str(task_id), "agent": "codex", "text": "  kept exactly  "},
        ))
        replay = port.execute(_call(
            "record-cook-log", run_id=run_id, request_id=request_id,
            arguments={"dish_id": str(task_id), "agent": "codex", "text": "  kept exactly  "},
        ))
        second = port.execute(_call(
            "record-cook-log", run_id=run_id, request_id=_next(ids),
            arguments={"dish_id": str(task_id), "agent": "codex", "text": "second"},
        ))

        assert first.ok and second.ok
        assert replay.ok and replay.request_replayed is True
        assert replay.data["log_id"] == first.data["log_id"]
        assert first.data["text"] == "  kept exactly  "
        assert first.data["content_version_id"] == str(before[0])
        assert first.data["dish_version"] == before[1]
        assert session.scalar(select(func.count()).select_from(wf.CookLogEntry)) == 2
        session.refresh(state)
        assert (state.current_content_version_id, state.dish_version, state.completed, state.archived_at) == before

        page1 = port.execute(_call(
            "cook-logs", run_id=run_id, principal="reader",
            arguments={"dish_id": str(task_id), "agent": "codex", "page_size": 1},
        ))
        assert page1.ok
        assert [row["log_id"] for row in page1.data["logs"]] == [first.data["log_id"]]
        first_log = page1.data["logs"][0]
        assert first_log["command_execution_id"]
        assert first_log["request_id"] == str(request_id)
        assert first_log["run_id"] == str(run_id)
        assert first_log["owner_id"] == "owner-1"
        assert first_log["principal_class"] == "agent"
        assert page1.data["next_cursor"]
        page2 = port.execute(_call(
            "cook-logs", run_id=run_id, principal="reader",
            arguments={"dish_id": str(task_id), "agent": "codex", "page_size": 1, "cursor": page1.data["next_cursor"]},
        ))
        assert [row["log_id"] for row in page2.data["logs"]] == [second.data["log_id"]]
        assert page2.data["next_cursor"] is None


def test_cook_log_text_bounds_fail_closed(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        for text in ("   ", "x" * 8001):
            result = port.execute(_call(
                "record-cook-log", run_id=run_id, request_id=_next(ids),
                arguments={"dish_id": str(task_id), "agent": "codex", "text": text},
            ))
            assert not result.ok
            assert result.code == "INVALID_ARGUMENT"
        assert session.scalar(select(func.count()).select_from(wf.CookLogEntry)) == 0


@pytest.mark.parametrize("lifecycle", ("open", "cooked", "archived"))
def test_record_cook_log_is_lifecycle_neutral(workflow_db, lifecycle: str) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        if lifecycle == "open":
            _start_initial(port, ids, task_id=task_id, run_id=run_id)
        elif lifecycle == "cooked":
            assert port.execute(_call("cooked", run_id=run_id, request_id=_next(ids), arguments={"dish_id": str(task_id), "agent": "codex"})).ok
        else:
            assert port.execute(_call("archive", run_id=run_id, request_id=_next(ids), arguments={"dish_id": str(task_id), "agent": "codex"})).ok
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state is not None
        before = (
            state.current_content_version_id,
            state.dish_version,
            state.completed,
            state.archived_at,
        )
        result = port.execute(_call(
            "record-cook-log", run_id=run_id, request_id=_next(ids),
            arguments={"dish_id": str(task_id), "agent": "codex", "text": lifecycle},
        ))
        assert result.ok, (result.code, result.data)
        assert result.data["content_version_id"] == str(before[0])
        assert result.data["dish_version"] == before[1]
        session.refresh(state)
        assert (
            state.current_content_version_id,
            state.dish_version,
            state.completed,
            state.archived_at,
        ) == before
        cooked = FrontendBoardQuery(session).archived_tasks(max_results=10).results
        if lifecycle == "cooked":
            assert [item.task_id for item in cooked] == [task_id]
            assert [[text for _, text in item.cook_logs] for item in cooked] == [["cooked"]]
        else:
            assert not cooked
