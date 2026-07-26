from __future__ import annotations

import copy
from pathlib import Path

import pytest

from dish_tool.errors import ReleaseResolutionError
from dish_tool.releases import resolve_release
from dish_tool.task_document import parse_task_document, validate_task_document


def _fixture_root() -> Path:
    return Path(__file__).parent / "fixtures" / "dish-version-current"


def test_runtime_validation_executes_resolved_schema():
    from test_dish_tool_step2_canonical import TASK
    release = resolve_release(_fixture_root(), protocol_role="research")
    document = parse_task_document(TASK)
    assert validate_task_document(document, expected_schema_version=release.schema_version, schema=release.schema).ok

    changed = copy.deepcopy(dict(release.schema))
    changed["task_document"] = copy.deepcopy(dict(changed["task_document"]))
    changed["task_document"]["allowed_statuses"] = ["pending-research"]
    result = validate_task_document(document, expected_schema_version=release.schema_version, schema=changed)
    assert any(item.rule == "state.status" for item in result.findings)


def test_protocol_path_cannot_escape_checkout(tmp_path):
    source = _fixture_root()
    for path in source.rglob("*"):
        target = tmp_path / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    outside = tmp_path.parent / "outside-protocol.md"
    outside.write_text("outside", encoding="utf-8")
    protocol = tmp_path / "dish-research-protocol.md"
    protocol.unlink()
    protocol.symlink_to(outside)

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(tmp_path, protocol_role="research")
    assert exc.value.rule == "honest_asset_outside_checkout"


def test_migration_path_cannot_escape_checkout(tmp_path):
    source = _fixture_root()
    for path in source.rglob("*"):
        target = tmp_path / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    schema_path = tmp_path / "dish-task-schema.json"
    import json
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["migration_files"] = ["../outside-migration.json"]
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    with pytest.raises(ReleaseResolutionError) as exc:
        resolve_release(tmp_path, include_migrations=True)
    assert exc.value.rule == "honest_asset_outside_checkout"
