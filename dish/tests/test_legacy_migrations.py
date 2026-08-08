import json
from pathlib import Path

from dish_tool.migrations import migrate_task_document


def test_authentic_schema1_fixture_executes_declared_handler():
    root = Path(__file__).parent / "fixtures"
    content = (root / "schema-1-authentic" / "task.txt").read_text()
    migration = json.loads((root / "dish-version-current" / "dish-schema-migrations" / "0002-canonical-document.json").read_text())
    schema = json.loads((root / "dish-version-current" / "dish-task-schema.json").read_text())
    result = migrate_task_document(content, migration, schema=schema)
    assert result.ok
    assert result.document.schema_version == "2"
    assert "Schema version: 2" in result.transformed_content
    assert "Status: pending-verification" in result.transformed_content


def test_unknown_or_manual_migration_is_quarantined():
    content = (Path(__file__).parent / "fixtures" / "schema-1-authentic" / "task.txt").read_text()
    result = migrate_task_document(content, {"from_schema_version": "1", "to_schema_version": "2", "operations": [{"type": "manual-reconciliation"}]})
    assert result.quarantined
    assert result.findings[0].rule == "migration.operation"
