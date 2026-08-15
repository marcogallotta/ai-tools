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
    "REVIEW PASSED",
    "INTEGRATION READY",
    "LOCAL REVIEW REQUIRED",
    "LOCAL IMPLEMENTATION COMPLETION REQUIRED",
    "LOCAL INTEGRATION CERTIFICATION REQUIRED",
    "BLOCKED",
    "WAITING ON DEPENDENCY",
    "MERGED",
)

REVIEW_STATUSES_WITH_WORKED_EXAMPLES = (
    "REVIEW PASSED",
    "INTEGRATION READY",
    "LOCAL REVIEW REQUIRED",
    "LOCAL IMPLEMENTATION COMPLETION REQUIRED",
    "LOCAL INTEGRATION CERTIFICATION REQUIRED",
)


def _review_contract_text() -> str:
    return (Path(__file__).parents[1] / "docs" / "agents" / "review.md").read_text(encoding="utf-8")


def _validate_review_handoff(message: str) -> str:
    status = message.splitlines()[0]
    assert status in REVIEW_FINAL_STATUSES
    assert "VERDICT:" not in message
    assert "Findings:" not in message
    if status not in ("MERGED", "WAITING ON DEPENDENCY"):
        assert "Action:" in message
    return status


def test_review_contract_exposes_the_canonical_final_handoff_statuses():
    contract = _review_contract_text()
    for status in REVIEW_FINAL_STATUSES:
        assert f"`{status}`" in contract
    for status in REVIEW_STATUSES_WITH_WORKED_EXAMPLES:
        assert f"```text\n{status}\n" in contract
    assert "Review itself does not merge" in contract
    assert "canonical repository is `marcogallotta/ai-tools`" in contract
    assert "Human output states the lifecycle result and one exact action only" in contract


def test_review_completion_handoffs_are_actionable_without_review_dump():
    examples = (
        "REVIEW PASSED\nPR #40 passed exact-head Review.\nWaiting for: GitHub exact-head certification.\nAction: none.",
        "INTEGRATION READY\nPR #40 passed Review and all required gates.\nAction: Integration may merge the exact reviewed head.",
        "LOCAL REVIEW REQUIRED\nPR #40 needs local Review evidence.\nAction: give PR #40 to a local Review agent; full handoff is on the PR.",
        "LOCAL IMPLEMENTATION COMPLETION REQUIRED\nPR #40 needs a semantic fix.\nAction: give PR #40 to an Implementation agent; full handoff is on the PR.",
        "LOCAL INTEGRATION CERTIFICATION REQUIRED\nPR #40 passed Review and needs local Integration certification.\nAction: give PR #40 to a local Integration agent; full handoff is on the PR.",
        "BLOCKED\nPR #40 blocked.\nAction: fix the PR-resident blocker\nReason: exact head fails the review invariant.",
        "WAITING ON DEPENDENCY\nPR #40 waiting on:\nordinary exact-head CI\nOwner: PR #40",
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
