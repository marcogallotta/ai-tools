from pathlib import Path

import pytest


from dish_tool.errors import DishRuleError
from dish_tool.models import (
    material_change_line,
    material_editor_line,
    validate_actor_model,
    validate_candidate_text,
    validate_change_reason,
)
from dish_tool.task_document import parse_task_document, validate_task_document
from tests.support.planning import Backend, TASK, app, write


UNSAFE_TEXT = [
    "bad\nmodel",
    "bad\rmodel",
    "bad\tmodel",
    "\nleading",
    "trailing\r",
    "bad\x00model",
    "bad\u200bmodel",
    "bad\u2028model",
    "bad\u2029model",
    "bad\ud800model",
]


@pytest.mark.parametrize("value", UNSAFE_TEXT)
def test_model_rejects_structural_unicode_before_trimming(value):
    with pytest.raises(DishRuleError) as caught:
        validate_actor_model(value)
    error = caught.value
    assert error.code == "INVALID_ARGUMENT"
    assert error.rule == "model_invalid_characters"
    assert error.retryable is True
    assert error.details == {"field": "model"}


@pytest.mark.parametrize("value", UNSAFE_TEXT)
def test_change_reason_rejects_structural_unicode_before_trimming(value):
    with pytest.raises(DishRuleError) as caught:
        validate_change_reason(value)
    error = caught.value
    assert error.code == "INVALID_ARGUMENT"
    assert error.rule == "change_reason_invalid_characters"
    assert error.retryable is True
    assert error.details == {"field": "change_reason"}


@pytest.mark.parametrize(
    "character",
    ["\x00", "\t", "\u200b", "\u2028", "\u2029", "\u202e"],
)
def test_candidate_text_rejects_unsafe_structural_characters(character):
    with pytest.raises(DishRuleError) as caught:
        validate_candidate_text(f"safe\ntext{character}hidden")
    error = caught.value
    assert error.code == "INVALID_ARGUMENT"
    assert error.rule == "candidate_text_invalid_characters"
    assert error.retryable is True
    assert error.details == {"field": "file_text"}


def test_candidate_text_preserves_canonical_newlines():
    text = "title\nbody\n"
    assert validate_candidate_text(text) == text


def test_audit_text_is_nfc_normalized_consistently():
    assert validate_actor_model("Cafe\u0301") == "Café"
    assert validate_change_reason("Cafe\u0301 route") == "Café route"
    assert material_editor_line("gpt", "Cafe\u0301", "2026-07-28") == (
        "Custom GPT — self-reported model: Café, 2026-07-28"
    )
    assert "self-reported model: Café" in material_change_line(
        "gpt",
        "Cafe\u0301",
        "2026-07-28",
        change="updated the candidate",
        reason="Café route",
        materiality="Large",
    )


def test_legacy_unlabelled_provenance_remains_parseable():
    document = parse_task_document(TASK)
    assert validate_task_document(document, expected_schema_version="2", schema={}).ok


@pytest.mark.parametrize("model", UNSAFE_TEXT)
def test_invalid_model_never_partially_mutates_task(tmp_path, model):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="initial",
        change_level=None,
        change_reason=None,
    )
    before_task = (backend.title, backend.notes, backend.section)
    before_counts = {
        table: application.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "write_attempts",
            "movement_attempts",
            "verification_cycles",
            "operation_steps",
        )
    }

    result = application.execute(
        "prepare",
        agent="gpt",
        model=model,
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "candidate.txt", TASK),
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"rule": "model_invalid_characters", "field": "model"}
    ]
    assert result["retryable"] is True
    assert (backend.title, backend.notes, backend.section) == before_task
    assert backend.writes == 0
    assert backend.moves == 0
    after_counts = {
        table: application.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before_counts
    }
    assert after_counts == before_counts


@pytest.mark.parametrize("reason", UNSAFE_TEXT + ["changes — forged field"])
def test_invalid_change_reason_creates_no_operation_or_effect(tmp_path, reason):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    before_task = (backend.title, backend.notes, backend.section)

    result = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="large",
        change_reason=reason,
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"rule": "change_reason_invalid_characters", "field": "change_reason"}
    ]
    assert result["retryable"] is True
    assert (backend.title, backend.notes, backend.section) == before_task
    assert backend.writes == 0
    assert backend.moves == 0
    for table in (
        "operations",
        "write_attempts",
        "movement_attempts",
        "verification_cycles",
        "operation_steps",
    ):
        assert application.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_non_change_start_preserves_forbidden_argument_diagnostic(tmp_path):
    backend = Backend()
    application = app(tmp_path, backend)
    result = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="planning",
        change_reason="bad\nreason",
    )
    assert result["errors"][0]["rule"] == "change_arguments_forbidden"
