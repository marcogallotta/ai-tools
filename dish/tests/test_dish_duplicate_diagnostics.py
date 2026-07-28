from __future__ import annotations

import pytest

from dish_tool.task_document import DocumentParseError, parse_planning_brief
from tests.test_dish_tool_step5_commands import Backend, TASK, app
from tests.test_dish_tool_step6_prepare import (
    Backend as TrackingBackend,
    app as planning_app,
)


def _read(tmp_path, content: str):
    lines = content.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    return app(tmp_path, backend).execute("read", agent="gpt", task_gid="t")


def _line_numbers(content: str, exact: str) -> list[int]:
    return [
        index
        for index, line in enumerate(content.splitlines(), start=1)
        if line == exact
    ]


def test_duplicate_state_error_names_field_and_reports_count_and_lines(tmp_path):
    content = TASK.replace(
        "Status: pending-verification",
        "Status: pending-verification\nStatus: ready",
    )
    result = _read(tmp_path, content)
    error = result["data"]["validation"][0]
    assert error == {
        "field": "Status",
        "lines": _line_numbers(content, "Status: pending-verification")
        + _line_numbers(content, "Status: ready"),
        "message": "duplicate state field Status",
        "occurrences": 2,
        "rule": "state_field_duplicate",
    }


def test_duplicate_section_error_names_normalized_heading(tmp_path):
    content = TASK.replace(
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n",
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n"
        "## WHAT TO BUY\nStill none\n",
    )
    result = _read(tmp_path, content)
    error = result["data"]["validation"][0]
    assert error == {
        "heading": "WHAT TO BUY",
        "lines": _line_numbers(content, "## WHAT TO BUY"),
        "message": "duplicate section WHAT TO BUY",
        "occurrences": 2,
        "rule": "section_duplicate",
    }


def test_duplicate_planning_error_names_label(tmp_path):
    content = TASK.replace(
        "Purpose: Compare texture",
        "Purpose: Compare texture\nPurpose: Compare aroma",
    )
    result = _read(tmp_path, content)
    error = result["data"]["validation"][0]
    assert error == {
        "field": "Purpose",
        "lines": _line_numbers(content, "Purpose: Compare texture")
        + _line_numbers(content, "Purpose: Compare aroma"),
        "message": "duplicate planning field Purpose",
        "occurrences": 2,
        "rule": "planning_field_duplicate",
    }


def test_duplicate_validation_aggregates_safe_structural_errors(tmp_path):
    content = TASK.replace(
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n",
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n"
        "## WHAT TO BUY\nStill none\n",
    ).replace(
        "Status: pending-verification",
        "Status: pending-verification\nStatus: ready",
    ).replace(
        "Purpose: Compare texture",
        "Purpose: Compare texture\nPurpose: Compare aroma",
    )
    result = _read(tmp_path, content)
    errors = result["data"]["validation"]
    assert [error["rule"] for error in errors] == [
        "section_duplicate",
        "state_field_duplicate",
        "planning_field_duplicate",
    ]
    assert [error.get("heading") or error.get("field") for error in errors] == [
        "WHAT TO BUY",
        "Status",
        "Purpose",
    ]


def test_planning_prepare_returns_rich_retryable_duplicate_error(tmp_path):
    backend = Backend()
    application = app(tmp_path, backend)
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="planning",
        change_level=None,
        change_reason=None,
    )
    candidate = tmp_path / "planning.txt"
    candidate.write_text(
        "Dish candidate: Test dish\n"
        "Purpose: Texture\n"
        "Purpose: Aroma\n"
        "Role: main\n"
        "Priors: None\n"
        "Locks: None\n"
        "Exemptions: None\n"
        "Research emphasis: Compare\n"
        "Destination section: Reference — 12345\n"
    )
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["retryable"] is True
    assert result["allowed_actions"] == ["prepare"]
    assert result["errors"] == [
        {
            "field": "Purpose",
            "lines": [2, 3],
            "message": "duplicate planning field Purpose",
            "occurrences": 2,
            "rule": "planning_field_duplicate",
        }
    ]
    assert backend.notes == ""


def test_standalone_planning_parser_exposes_duplicate_details():
    content = (
        "Dish candidate: Test dish\n"
        "Purpose: Texture\n"
        "Purpose: Aroma\n"
        "Role: main\n"
        "Priors: None\n"
        "Locks: None\n"
        "Exemptions: None\n"
        "Research emphasis: Compare\n"
        "Destination section: Reference — 12345\n"
    )
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(content)
    assert exc.value.rule == "planning_field_duplicate"
    assert exc.value.details == {
        "field": "Purpose",
        "occurrences": 2,
        "lines": [2, 3],
    }


def test_case_insensitive_planning_duplicate_is_rejected_before_execution_claim(tmp_path):
    backend = TrackingBackend()
    application = planning_app(tmp_path, backend)
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="planning",
        change_level=None,
        change_reason=None,
    )
    operation_id = started["submission_id"]
    candidate = tmp_path / "planning-case-duplicate.txt"
    candidate.write_text(
        "Dish candidate: Test dish\n"
        "Purpose: Texture\n"
        "purpose: Aroma\n"
        "Role: main\n"
        "Priors: None\n"
        "Locks: None\n"
        "Exemptions: None\n"
        "Research emphasis: Compare\n"
        "Destination section: Reference — 12345\n"
    )

    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=str(candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"] == [
        {
            "field": "Purpose",
            "lines": [2, 3],
            "message": "duplicate planning field Purpose",
            "occurrences": 2,
            "rule": "planning_field_duplicate",
        }
    ]
    assert backend.writes == 0
    assert backend.moves == 0
    assert tuple(
        application.conn.execute(
            "SELECT status,phase FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    ) == ("open", "prepare_required")
    for table in (
        "operation_steps",
        "write_attempts",
        "movement_attempts",
        "operation_executions",
        "operation_execution_claims",
    ):
        assert application.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE operation_id=?", (operation_id,)
        ).fetchone()[0] == 0, table
