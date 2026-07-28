from __future__ import annotations

import pytest

from dish_tool.task_document import DocumentParseError, parse_planning_brief
from tests.test_dish_tool_step6_prepare import Backend, PLANNING, app, write


@pytest.mark.parametrize(
    "disguised_label",
    [
        "Purpose\u00a0: Compare aroma",
        "Ｐｕｒｐｏｓｅ: Compare aroma",
        "Pur\u200bpose: Compare aroma",
        "Purpose： Compare aroma",
    ],
)
def test_unicode_disguised_planning_duplicate_is_rejected_before_any_mutation(
    tmp_path, disguised_label
):
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
    operation_id = started["submission_id"]
    candidate = PLANNING.replace(
        "Purpose: Compare texture\n",
        f"Purpose: Compare texture\n{disguised_label}\n",
    )

    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=write(tmp_path, "unicode-disguised-planning.txt", candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"] == [
        {
            "rule": "planning_field_duplicate",
            "field": "Purpose",
            "occurrences": 2,
            "lines": [3, 4],
            "message": "duplicate planning field Purpose",
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

    inspected = application.execute(
        "inspect", agent="gpt", submission_id=operation_id
    )
    view = inspected["data"]["authoritative_view"]
    assert view["recovery_required"] is False
    assert inspected["allowed_actions"] == ["prepare"]


@pytest.mark.parametrize(
    "disguised_label",
    [
        "Purpose\u00a0: Compare aroma",
        "Ｐｕｒｐｏｓｅ: Compare aroma",
        "Pur\u200bpose: Compare aroma",
        "Purpose： Compare aroma",
    ],
)
def test_standalone_parser_rejects_unicode_disguised_duplicates(disguised_label):
    candidate = PLANNING.replace(
        "Purpose: Compare texture\n",
        f"Purpose: Compare texture\n{disguised_label}\n",
    )
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)

    assert exc.value.rule == "planning_field_duplicate"
    assert exc.value.details == {
        "field": "Purpose",
        "occurrences": 2,
        "lines": [3, 4],
    }


def test_lone_compatibility_colon_label_is_rejected_directly():
    candidate = PLANNING.replace(
        "Purpose: Compare texture", "Purpose： Compare texture"
    )
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)

    assert exc.value.rule == "planning_field_label_disguised"
    assert exc.value.details == {
        "field": "Purpose",
        "canonical_label": "Purpose",
        "line": 3,
    }
