from pathlib import Path


def test_responsibility_modules_expose_authoritative_owners():
    from dish_tool import (
        command_support,
        database_initialization,
        database_migrations,
        database_schema,
        database_schema_validation,
        schema_validation,
    )
    from dish_tool.database_initialization import initialize_database
    from dish_tool.constants import DEFAULT_DB_PATH
    from dish_tool.database_migrations import migrate_database
    from dish_tool.database_schema import MIGRATIONS
    from dish_tool.database_schema_validation import validate_current_database
    from dish_tool.schema_validation import validate_manifest_shape

    assert command_support.CommandTrace
    assert schema_validation.validate_manifest_shape is validate_manifest_shape
    assert database_schema.MIGRATIONS is MIGRATIONS
    assert database_migrations.migrate_database is migrate_database
    assert not hasattr(database_schema, "migrate_database")
    assert database_schema_validation.validate_current_database is validate_current_database
    assert not hasattr(database_schema, "validate_current_database")
    assert database_initialization.initialize_database is initialize_database
    assert database_initialization.DEFAULT_DB_PATH is DEFAULT_DB_PATH
    assert not hasattr(database_schema, "DEFAULT_DB_PATH")
    assert not hasattr(database_schema, "initialize_database")


def test_operational_reports_distinguish_recovery_and_movement_purpose():
    reports = (Path(__file__).parents[1] / "dish-reports.sql").read_text()
    assert "movement_outcomes_by_purpose" in reports
    assert "destination_submission" in reports
    assert "recovery_reconciliations" in reports
    assert "invalid_final_movement_semantics" in reports

REVIEW_FINAL_STATUSES = (
    "READY FOR MERGE",
    "LOCAL AGENT REQUIRED",
    "BLOCKED",
    "WAITING ON DEPENDENCY",
)


def _review_contract_text() -> str:
    return (Path(__file__).parents[1] / "docs" / "agents" / "review.md").read_text(encoding="utf-8")


def _validate_review_handoff(message: str) -> str:
    status = message.splitlines()[0]
    assert status in REVIEW_FINAL_STATUSES
    assert "VERDICT:" not in message
    assert "Findings:" not in message
    if status == "READY FOR MERGE":
        assert "Reason: Review passed, no local work required." in message
        return "handoff to Integration"
    if status == "LOCAL AGENT REQUIRED":
        assert "\nAction:\n" in message
        assert "\nEffort:\n" in message
        return message.split("\nAction:\n", 1)[1].split("\n\nEffort:\n", 1)[0]
    if status == "BLOCKED":
        assert "\nAction:\n" in message
        assert "\nReason:\n" in message
        return message.split("\nAction:\n", 1)[1].split("\n\nReason:\n", 1)[0]
    assert "\nOwner:\n" in message
    return message.split("\nPR #", 1)[1].split(" waiting on:\n", 1)[1].split("\n\nOwner:\n", 1)[0]


def test_review_contract_exposes_only_four_action_handoff_statuses():
    contract = _review_contract_text()
    for status in REVIEW_FINAL_STATUSES:
        assert f"```text\n{status}\n" in contract
    assert "Review does not merge or integrate the PR." in contract
    assert "canonical repository is `marcogallotta/ai-tools`" in contract
    assert "no preamble, epilogue, verdict dump" in contract


def test_review_completion_handoffs_are_actionable_without_review_dump():
    examples = (
        "READY FOR MERGE\n\nPR #40 is ready for merge.\nReason: Review passed, no local work required.",
        "LOCAL AGENT REQUIRED\n\nPR #40 requires local agent.\n\nAction:\nrun exact-check\n\nEffort:\nSmall",
        "BLOCKED\n\nPR #40 blocked.\n\nAction:\nfix the PR-resident blocker\n\nReason:\nExact head fails the review invariant.",
        "WAITING ON DEPENDENCY\n\nPR #40 waiting on:\nordinary exact-head CI\n\nOwner:\nPR #40",
    )
    assert [_validate_review_handoff(message) for message in examples]


def test_review_completion_handoff_rejects_missing_action_or_reasoning_dump():
    import pytest

    with pytest.raises(AssertionError):
        _validate_review_handoff("BLOCKED\n\nPR #40 blocked.\n\nReason:\nNeeds work.")
    with pytest.raises(AssertionError):
        _validate_review_handoff(
            "READY FOR MERGE\n\nPR #40 is ready for merge.\nReason: Review passed, no local work required.\n\nFindings:\n- long review dump"
        )
