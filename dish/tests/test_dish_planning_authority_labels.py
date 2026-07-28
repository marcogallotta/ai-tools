from __future__ import annotations

import pytest

from dish_tool.task_document import DocumentParseError, parse_planning_brief
from tests.test_dish_tool_step6_prepare import Backend, PLANNING, app, write


def _with_extra_label(label: str) -> str:
    return PLANNING.replace(
        "Purpose: Compare texture\n",
        f"Purpose: Compare texture\n{label}: Compare aroma\n",
    )


def test_case_variant_planning_label_is_detected_as_duplicate():
    candidate = _with_extra_label("purpose")
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)
    assert exc.value.rule == "planning_field_duplicate"
    assert exc.value.details == {
        "field": "Purpose",
        "occurrences": 2,
        "lines": [3, 4],
    }


def test_single_case_variant_planning_label_is_rejected_not_canonicalized():
    candidate = PLANNING.replace("Purpose: Compare texture", "purpose: Compare texture")
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)
    assert exc.value.rule == "planning_field_label_noncanonical"
    assert exc.value.details == {
        "field": "Purpose",
        "label": "purpose",
        "canonical_label": "Purpose",
        "line": 3,
    }


def test_case_variant_duplicate_fails_before_planning_write(tmp_path):
    backend = Backend()
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "case-duplicate.txt", _with_extra_label("purpose")),
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "planning_field_duplicate"
    assert backend.writes == 0
    assert backend.moves == 0
    assert application.conn.execute(
        "SELECT COUNT(*) FROM operation_steps WHERE operation_id=?",
        (started["submission_id"],),
    ).fetchone()[0] == 0
