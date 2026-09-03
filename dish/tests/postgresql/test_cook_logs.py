from __future__ import annotations

import uuid

import pytest
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import (
    postgres_action_argument_schema,
    validate_postgres_action_request,
)
from dish_pg.database import session_scope
from dish_service.cli import build_parser
from sqlalchemy import func, select

from tests.support.postgresql.command import _call, _port, _start_initial
from tests.support.postgresql.workflow import _next, _register_run


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
