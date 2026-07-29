import json
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN_DIR))

from dish_tool.migrations import migrate_task_document
from dish_tool.task_document import (
    FindingKind,
    PLANNING_FIELDS,
    parse_planning_brief,
    parse_task_document,
    validate_planning_brief,
    validate_task_document,
)


PLANNING = """Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
"""

TASK = """[non-main] Test dish — crisp comparison side
A compact side dish for testing texture.
WHY COOK IT
Compare hydration routes.
## WHAT TO BUY
None - pantry snapshot lists required items in stock
## QUANTITIES
Portions: one sitting
100 g test ingredient
### Mise en place
Keep dry.
## HOW TO COOK IT
1. Cook it.
## WHAT SUCCESS LOOKS LIKE
Crisp and aromatic.
---
## PROCESS RECORD
Status: pending-verification
Status detail: None
Resume status: None
Verification protocol release: abc123
Researched by: ChatGPT — GPT-5, 2026-07-25
Verified by: None
Self-verified: ChatGPT — GPT-5, 2026-07-25
### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
### Decisions
Human — Marco: Use the smaller batch, 2026-07-25, to isolate texture
### Research basis
Classification: Source-backed dish
source.example/test — Construction — hydration ratio — selected route is drier
### Material changes
2026-07-25 — ChatGPT — GPT-5 — tightened hydration — improve crispness — Large — pending-verification
Schema version: 2
"""


def test_planning_brief_round_trip_has_exact_eight_fields():
    brief = parse_planning_brief(PLANNING)
    assert list(brief.values) == [
        "Dish candidate", "Purpose", "Role", "Priors", "Locks", "Exemptions",
        "Research emphasis", "Destination section",
    ]
    assert parse_planning_brief(brief.render()).values == brief.values


def test_planning_brief_rejects_unsupported_field_instead_of_absorbing_it():
    candidate = (
        f"{PLANNING.rstrip()}\n"
        "Serving note: hidden unsupported field\n"
    )

    with pytest.raises(ValueError) as exc_info:
        parse_planning_brief(candidate)

    assert getattr(exc_info.value, "rule") == "planning_field_unknown"
    assert getattr(exc_info.value, "details") == {
        "field": "Serving note",
        "line": 9,
    }


@pytest.mark.parametrize("field_name", PLANNING_FIELDS)
def test_planning_brief_rejects_empty_required_values(field_name):
    candidate = "\n".join(
        f"{field_name}:" if line.startswith(f"{field_name}:") else line
        for line in PLANNING.splitlines()
    )

    validation = validate_planning_brief(parse_planning_brief(candidate))

    assert [
        (finding.rule, finding.location, finding.message)
        for finding in validation.findings
    ] == [
        (
            "planning.field-empty",
            field_name,
            f"{field_name} requires a non-empty value",
        )
    ]


def test_planning_brief_rejects_unsupported_exemption_tags():
    candidate = PLANNING.replace(
        "Exemptions: None",
        "Exemptions: [nutrition-sodium] — Marco approved for this dish",
    )

    validation = validate_planning_brief(parse_planning_brief(candidate))

    assert [
        (finding.rule, finding.location, finding.message)
        for finding in validation.findings
    ] == [
        (
            "planning.exemption-tag-unsupported",
            "Exemptions",
            (
                "Unsupported exemption tags: [nutrition-sodium]; allowed tags are "
                "[nutrition-kcal], [nutrition-protein], [nutrition-fat]"
            ),
        )
    ]


@pytest.mark.parametrize(
    "value",
    [
        "None",
        "[nutrition-kcal] — Marco approved for this dish",
        "[nutrition-protein] [nutrition-fat] — Marco approved for this dish",
    ],
)
def test_planning_brief_accepts_supported_exemption_tags(value):
    candidate = PLANNING.replace("Exemptions: None", f"Exemptions: {value}")

    validation = validate_planning_brief(parse_planning_brief(candidate))

    assert not [
        finding for finding in validation.findings
        if finding.location == "Exemptions"
    ]


def test_complete_task_round_trip_and_lower_heading():
    document = parse_task_document(TASK)
    assert document.nutrition_scope == "out-of-scope"
    assert "### Mise en place" in document.sections["QUANTITIES"]
    assert parse_task_document(document.render()) == document
    assert validate_task_document(document, expected_schema_version="2").ok


def test_complete_task_rejects_empty_recognition_line():
    candidate = TASK.replace(
        "A compact side dish for testing texture.",
        "",
        1,
    )

    validation = validate_task_document(parse_task_document(candidate))

    assert [
        (finding.rule, finding.kind, finding.message, finding.location)
        for finding in validation.findings
    ] == [
        (
            "document.recognition-empty",
            FindingKind.SYNTAX,
            "recognition line requires non-empty text",
            "recognition",
        )
    ]


@pytest.mark.parametrize(
    "replacement",
    [
        "",
        "Portions:",
    ],
)
def test_complete_task_requires_nonempty_portions_line(replacement):
    candidate = TASK.replace("Portions: one sitting", replacement, 1)

    validation = validate_task_document(parse_task_document(candidate))

    assert [
        (finding.rule, finding.kind, finding.message, finding.location)
        for finding in validation.findings
    ] == [
        (
            "quantities.portions-required",
            FindingKind.SYNTAX,
            "QUANTITIES requires a non-empty Portions: line",
            "QUANTITIES",
        )
    ]


def test_complete_task_rejects_duplicate_closing_schema_version():
    candidate = TASK.replace(
        "Schema version: 2\n",
        "Schema version: 2\nSchema version: 2\n",
        1,
    )
    schema_lines = [
        index
        for index, line in enumerate(candidate.splitlines(), start=1)
        if line == "Schema version: 2"
    ]

    with pytest.raises(ValueError) as exc_info:
        parse_task_document(candidate)

    assert getattr(exc_info.value, "rule") == "schema_version_duplicate"
    assert getattr(exc_info.value, "details") == {
        "occurrences": 2,
        "lines": schema_lines,
    }


def test_all_canonical_actor_names_and_verified_material_change_are_valid():
    for actor in ("ChatGPT", "Custom GPT", "Claude", "Codex"):
        candidate = TASK.replace(
            "Researched by: ChatGPT — GPT-5, 2026-07-25",
            f"Researched by: {actor} — model-name, 2026-07-25",
        ).replace(
            "Self-verified: ChatGPT — GPT-5, 2026-07-25",
            f"Self-verified: {actor} — model-name, 2026-07-25",
        ).replace(
            "2026-07-25 — ChatGPT — GPT-5 — tightened hydration — improve crispness — Large — pending-verification",
            f"2026-07-25 — {actor} — model-name — tightened hydration — improve crispness — Large — verified — {actor}, model-name, 2026-07-25",
        )
        assert validate_task_document(parse_task_document(candidate)).ok


def test_bare_gpt_and_legacy_material_change_grammar_are_rejected():
    bare = TASK.replace(
        "Researched by: ChatGPT — GPT-5, 2026-07-25",
        "Researched by: GPT — GPT-5, 2026-07-25",
    )
    assert any(
        finding.rule == "state.actor-format"
        for finding in validate_task_document(parse_task_document(bare)).findings
    )
    legacy = TASK.replace(
        "2026-07-25 — ChatGPT — GPT-5 — tightened hydration — improve crispness — Large — pending-verification",
        "2026-07-25 — ChatGPT/GPT-5 — tightened hydration — improve crispness — material — not independently verified",
    )
    assert any(
        finding.rule == "material-changes.format"
        for finding in validate_task_document(parse_task_document(legacy)).findings
    )


def test_exact_once_state_fields_reject_duplicate():
    bad = TASK.replace("Status: pending-verification", "Status: pending-verification\nStatus: ready")
    try:
        parse_task_document(bad)
    except ValueError as exc:
        assert getattr(exc, "rule") == "state_field_duplicate"
    else:
        raise AssertionError("duplicate state field accepted")


def test_illegal_status_combination_is_distinct():
    document = parse_task_document(TASK.replace("Status detail: None", "Status detail: still working"))
    result = validate_task_document(document)
    assert result.by_kind(FindingKind.ILLEGAL_COMBINATION)


def test_extra_top_level_section_fails_but_lower_heading_is_allowed():
    bad = TASK.replace("## HOW TO COOK IT", "## EXTRA\nNo.\n## HOW TO COOK IT")
    try:
        parse_task_document(bad)
    except ValueError as exc:
        assert getattr(exc, "rule") == "top_level_section_unknown"
    else:
        raise AssertionError("extra top-level section accepted")


def test_complete_task_rejects_unsupported_planning_field():
    candidate = TASK.replace(
        "Destination section: Sichuan — 12345",
        (
            "Destination section: Sichuan — 12345\n"
            "Serving note: hidden unsupported field"
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        parse_task_document(candidate)

    assert getattr(exc_info.value, "rule") == "planning_field_unknown"
    assert getattr(exc_info.value, "details")["field"] == "Serving note"


def test_schema_migration_executes_declared_target_version_handler():
    source = TASK.replace("Schema version: 2", "Schema version: 1")
    migration = json.loads((BIN_DIR / "tests" / "fixtures" / "dish-version-current" / "dish-schema-migrations" / "0002-canonical-document.json").read_text())
    result = migrate_task_document(source, migration)
    assert result.ok
    assert result.document.schema_version == "2"
    assert "Schema version: 2" in result.transformed_content
    assert "Schema version: 1" not in result.transformed_content


def test_ambiguous_legacy_content_is_quarantined():
    migration = {"from_schema_version": "1", "to_schema_version": "2"}
    result = migrate_task_document("legacy free text", migration)
    assert result.quarantined
    assert result.findings[0].kind is FindingKind.SEMANTIC_REVIEW
