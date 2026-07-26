from pathlib import Path


def test_responsibility_modules_and_compatibility_facades_exist():
    from dish_tool import command_support, content_validation, database_schema, schema_validation
    from dish_tool.database import MIGRATIONS, initialize_database
    from dish_tool.validation import validate_manifest_shape, validate_note

    assert command_support.CommandTrace
    assert content_validation.validate_note is validate_note
    assert schema_validation.validate_manifest_shape is validate_manifest_shape
    assert database_schema.MIGRATIONS is MIGRATIONS
    assert database_schema.initialize_database is initialize_database


def test_operational_reports_distinguish_recovery_and_movement_purpose():
    reports = (Path(__file__).parents[1] / "dish-reports.sql").read_text()
    assert "movement_outcomes_by_purpose" in reports
    assert "destination_submission" in reports
    assert "recovery_reconciliations" in reports
    assert "invalid_final_movement_semantics" in reports
