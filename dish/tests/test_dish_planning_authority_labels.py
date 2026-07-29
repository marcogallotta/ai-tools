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


def test_zero_width_space_planning_label_is_detected_as_duplicate():
    candidate = _with_extra_label("Pur\u200bpose")
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)
    assert exc.value.rule == "planning_field_duplicate"
    assert exc.value.details == {
        "field": "Purpose",
        "occurrences": 2,
        "lines": [3, 4],
    }


def test_lone_zero_width_space_planning_label_is_rejected_directly():
    candidate = PLANNING.replace("Purpose: Compare texture", "Pur\u200bpose: Compare texture")
    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)
    assert exc.value.rule == "planning_field_label_format_character"
    assert exc.value.details == {
        "field": "Purpose",
        "label": "Pur\u200bpose",
        "canonical_label": "Purpose",
        "line": 3,
    }


def test_zero_width_space_duplicate_fails_before_planning_write(tmp_path):
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
        file_path=write(tmp_path, "zero-width-duplicate.txt", _with_extra_label("Pur\u200bpose")),
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "planning_field_duplicate"
    assert backend.writes == 0
    assert backend.moves == 0


def test_case_variant_process_subheading_reports_direct_canonical_diagnostic():
    from tests.test_dish_tool_step2_canonical import TASK
    from dish_tool.task_document import parse_task_document

    candidate = TASK.replace("### Research basis", "### Research Basis")
    with pytest.raises(DocumentParseError) as exc:
        parse_task_document(candidate)
    assert exc.value.rule == "process_subheading_noncanonical"
    assert exc.value.details == {
        "heading": "### Research Basis",
        "canonical_heading": "### Research basis",
        "line": candidate.splitlines().index("### Research Basis") + 1,
    }


@pytest.mark.parametrize("heading", ["### planning brief", "### PLANNING BRIEF"])
def test_case_variant_heading_in_planning_candidate_reports_direct_diagnostic(
    heading,
):
    candidate = f"{PLANNING.rstrip()}\n{heading}\nHidden continuation text.\n"

    with pytest.raises(DocumentParseError) as exc:
        parse_planning_brief(candidate)

    assert exc.value.rule == "process_subheading_noncanonical"
    assert exc.value.details == {
        "heading": heading,
        "canonical_heading": "### Planning brief",
        "line": 10,
    }


@pytest.mark.parametrize("heading", ["### planning brief", "### PLANNING BRIEF"])
def test_case_variant_heading_in_planning_prepare_fails_before_write(
    tmp_path, heading,
):
    backend = Backend()
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    candidate = f"{PLANNING.rstrip()}\n{heading}\nHidden continuation text.\n"

    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "case-heading.txt", candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["retryable"] is True
    assert result["allowed_actions"] == ["prepare"]
    assert result["errors"] == [
        {
            "rule": "process_subheading_noncanonical",
            "heading": heading,
            "canonical_heading": "### Planning brief",
            "line": 10,
            "message": (
                f"non-canonical process subheading {heading}; "
                "use ### Planning brief"
            ),
        }
    ]
    assert backend.writes == 0
    assert backend.moves == 0


def test_case_variant_process_subheading_is_detected_as_duplicate():
    from tests.test_dish_tool_step2_canonical import TASK
    from dish_tool.task_document import parse_task_document

    candidate = TASK.replace(
        "### Research basis",
        "### Research basis\n### Research Basis",
    )
    with pytest.raises(DocumentParseError) as exc:
        parse_task_document(candidate)
    assert exc.value.rule == "process_subheading_duplicate"
    assert exc.value.details == {
        "heading": "### Research basis",
        "occurrences": 2,
        "lines": [
            candidate.splitlines().index("### Research basis") + 1,
            candidate.splitlines().index("### Research Basis") + 1,
        ],
    }


def test_case_variant_top_level_heading_reports_direct_diagnostic():
    from tests.test_dish_tool_step2_canonical import TASK
    from dish_tool.task_document import parse_task_document

    candidate = TASK.replace("## QUANTITIES", "## quantities")
    with pytest.raises(DocumentParseError) as exc:
        parse_task_document(candidate)
    assert exc.value.rule == "section_heading_noncanonical"
    assert exc.value.details == {
        "heading": "## quantities",
        "canonical_heading": "## QUANTITIES",
        "line": candidate.splitlines().index("## quantities") + 1,
    }


def test_case_variant_process_heading_reports_direct_diagnostic():
    from tests.test_dish_tool_step2_canonical import TASK
    from dish_tool.task_document import parse_task_document

    candidate = TASK.replace("## PROCESS RECORD", "## Process Record")
    with pytest.raises(DocumentParseError) as exc:
        parse_task_document(candidate)
    assert exc.value.rule == "process_heading_noncanonical"
    assert exc.value.details == {
        "heading": "## Process Record",
        "canonical_heading": "## PROCESS RECORD",
        "line": candidate.splitlines().index("## Process Record") + 1,
    }
