from pathlib import Path

import pytest


from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database_initialization import initialize_database
from dish_tool.models import ResolvedRelease
from tests.support.planning import (
    Backend,
    PLANNING,
    TASK,
    app,
    release,
    write,

)

_DUPLICATE_SCHEMA_CANDIDATE = TASK.replace(
    "Schema version: 2\n",
    "Schema version: 2\nSchema version: 2\n",
    1,
)

@pytest.mark.smoke
def test_planning_prepare_writes_live_and_preserves_research_queue(tmp_path):
    b=Backend(); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="planning",change_level=None,change_reason=None)
    result=a.execute("prepare", model="gpt-5.6-sol",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"p.txt",PLANNING))
    assert result["ok"] and b.writes == 1 and b.section == "rq"
    assert "Locks: Keep crisp" in b.notes and "Exemptions: None" in b.notes
    assert result["allowed_actions"] == ["start"]
    assert result["data"]["required_start_kind"] == "initial"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
    ]
@pytest.mark.smoke
@pytest.mark.parametrize("kind", ["planning", "initial"])
@pytest.mark.parametrize("character", ["\x00", "\u200b", "\u202e"])
def test_prepare_rejects_unsafe_candidate_text_before_mutation(
    tmp_path, kind, character
):
    if kind == "planning":
        b = Backend()
        candidate = PLANNING.replace(
            "Purpose: Compare texture",
            f"Purpose: Compare{character} texture",
        )
    else:
        lines = TASK.splitlines()
        b = Backend(lines[0], "\n".join(lines[1:]) + "\n")
        candidate = TASK.replace(
            "Compare hydration routes.",
            f"Compare{character} hydration routes.",
        )
    a = app(tmp_path, b)
    started = a.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind=kind,
        change_level=None,
        change_reason=None,
    )

    result = a.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "candidate.txt", candidate),
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"rule": "candidate_text_invalid_characters", "field": "file_text"}
    ]
    assert result["retryable"] is True
    assert b.writes == 0
    assert b.moves == 0
    assert (
        a.conn.execute("SELECT COUNT(*) FROM write_attempts").fetchone()[0] == 0
    )
@pytest.mark.smoke
def test_research_prepare_writes_pending_then_moves_and_freezes_cycle(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    result=a.execute("prepare", agent="gpt",model="gpt-5.6-sol",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["ok"] and b.writes == 1 and b.moves == 1 and b.section == "vq"
    assert "Status: pending-verification" in b.notes
    assert "Verification protocol release: sha256:" in b.notes
    assert result["data"]["verification_cycle"]["protocol_release"].startswith("sha256:")
    assert result["allowed_actions"] == ["start"]
    assert result["data"]["required_start_kind"] == "verification"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
    ]
    assert "agent-semantic-review" not in result["data"]["validation_scope"]
    normalization = result["data"]["content_normalization"]
    assert normalization["applied"] is True
    assert {
        "Status", "Status detail", "Verification protocol release",
        "Researched by", "Self-verified",
    }.issubset(normalization["tool_owned_fields"])
    assert normalization["submitted_candidate_identity_is_authoritative"] is False
    assert "after these tool-owned process-field normalizations" in normalization["identity_scope"]
    verification = a.execute(
        "start", agent="codex", task_gid="t", kind="verification",
        run_id="fresh-verification-run",
        independence_attestation="independent",
    )
    assert verification["ok"]
    assert verification["allowed_actions"] == ["inspect"]
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("candidate", "filename", "expected_error"),
    [
        (
            TASK.replace("A compact side dish for testing texture.", "", 1),
            "empty-recognition.txt",
            {
                "rule": "document.recognition-empty",
                "kind": "syntax",
                "message": "canonical line 2 requires a non-empty dish-summary/meal-role sentence",
                "location": {"section": "canonical-header", "line": 2, "after": "title"},
                "current": {
                    "line_1": "[non-main] Test dish — crisp comparison side",
                    "line_2": "",
                },
                "expected": {
                    "line": 2,
                    "syntax": "<what the dish is, how it eats, and its meal role>",
                },
                "example": [
                    "Dish name — short identity phrase",
                    "A concise sentence describing what it is, how it eats, and its meal role.",
                ],
                "recovery": "Insert one non-empty dish-summary sentence immediately after the title line.",
            },
        ),
        (
            TASK.replace("Portions: one sitting\n", "", 1),
            "missing-portions.txt",
            {
                "rule": "quantities.portions-required",
                "kind": "syntax",
                "message": "QUANTITIES requires a non-empty Portions: line",
                "location": "QUANTITIES",
                "current": None,
                "expected": "Portions: <non-empty serving count or yield>",
                "example": "Portions: 2",
                "recovery": "Add or complete a non-empty `Portions:` line inside QUANTITIES.",
            },
        ),
        (
            _DUPLICATE_SCHEMA_CANDIDATE,
            "duplicate-schema-version.txt",
            {
                "rule": "schema_version_duplicate",
                "message": "duplicate closing Schema version",
                "occurrences": 2,
                "lines": [
                    index
                    for index, line in enumerate(
                        _DUPLICATE_SCHEMA_CANDIDATE.splitlines(), start=1
                    )
                    if line == "Schema version: 2"
                ],
            },
        ),
    ],
)
def test_research_prepare_rejects_invalid_document_before_write(
    tmp_path, candidate, filename, expected_error
):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None,
    )

    result = application.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, filename, candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"] == [expected_error]
    assert backend.writes == 0
    assert backend.moves == 0
@pytest.mark.smoke
def test_planning_prepare_reports_every_missing_field_and_required_label(tmp_path):
    b = Backend("Planning task", "")
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    incomplete = PLANNING.replace("Research emphasis: Compare two hydration levels\n", "").replace(
        "Destination section: Sichuan — 12345\n", ""
    )
    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "missing-planning.txt", incomplete),
    )
    assert result["code"] == "VALIDATION_FAILED"
    missing = [
        item for item in result["errors"]
        if item.get("rule") == "planning_field_missing" and "field" in item
    ]
    assert missing == [
        {
            "rule": "planning_field_missing",
            "field": "Research emphasis",
            "required_label": "Research emphasis: <value>",
        },
        {
            "rule": "planning_field_missing",
            "field": "Destination section",
            "required_label": "Destination section: <value>",
        },
    ]
@pytest.mark.smoke
def test_planning_prepare_rejects_unsupported_field_before_write(tmp_path):
    b = Backend("Planning task", "")
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    candidate = (
        f"{PLANNING.rstrip()}\n"
        "Serving note: hidden unsupported field\n"
    )

    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "unsupported-planning-field.txt", candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"] == [
        {
            "rule": "planning_field_unknown",
            "message": "unsupported planning field: Serving note",
            "field": "Serving note",
            "line": 10,
        }
    ]
    assert b.writes == 0
@pytest.mark.smoke
@pytest.mark.parametrize("field_name", ["Dish candidate", "Purpose", "Priors"])
def test_planning_prepare_rejects_empty_required_values_before_write(
    tmp_path, field_name
):
    b = Backend("Planning task", "")
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    candidate = "\n".join(
        f"{field_name}:" if line.startswith(f"{field_name}:") else line
        for line in PLANNING.splitlines()
    )

    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "empty-planning-field.txt", candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"] == [
        {
            "rule": "planning.field-empty",
            "kind": "syntax",
            "message": f"{field_name} requires a non-empty value",
            "location": field_name,
            "current": None,
            "expected": "<field name>: <non-empty value>",
            "recovery": "Populate the reported Planning brief field with a non-empty value.",
        }
    ]
    assert result["data"]["retry"] == {
        "mode": "correct_then_retry",
        "action": "prepare",
        "same_operation": True,
        "same_cycle": False,
        "fresh_request_id": True,
        "mutation_occurred": False,
        "instruction": "Correct the submitted candidate using the validation findings, then retry `prepare` on this same open operation.",
    }
    assert b.writes == 0
@pytest.mark.smoke
def test_planning_prepare_rejects_unsupported_exemption_before_write(tmp_path):
    b = Backend("Planning task", "")
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    candidate = PLANNING.replace(
        "Exemptions: None",
        "Exemptions: [nutrition-sodium] — Marco approved for this dish",
    )

    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "unsupported-exemption.txt", candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "planning.exemption-tag-unsupported"
    assert result["errors"][0]["location"] == "Exemptions"
    assert b.writes == 0
