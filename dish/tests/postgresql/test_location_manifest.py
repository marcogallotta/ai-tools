from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dish_pg import location_manifest as manifest_module
from dish_pg.importer import iter_source
from dish_pg.legacy_source import export_legacy_source
from dish_pg.location_manifest import (
    ASANA_IDENTITY_NAMESPACE,
    LocationManifestError,
    _atomic_json,
    _environment_file,
    build_location_manifest,
    target_uuid,
)


def _database(path, *task_gids: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE task_content_state(
        task_gid TEXT PRIMARY KEY,last_confirmed_identity TEXT,last_confirmed_title TEXT,
        last_confirmed_notes TEXT,schema_version TEXT,confirmed_at TEXT)"""
    )
    for index, task_gid in enumerate(task_gids, 1):
        connection.execute(
            "INSERT INTO task_content_state VALUES (?,?,?,?,?,?)",
            (
                task_gid,
                f"identity-{index}",
                f"Title {index}",
                f"Body {index}",
                "schema-1",
                "2026-08-03T09:00:00+00:00",
            ),
        )
    connection.commit()
    connection.close()


def _task(task_gid: str, project_gid: str, section_gid: str, *, completed: bool) -> dict:
    return {
        "gid": task_gid,
        "completed": completed,
        "memberships": [
            {
                "project": {"gid": project_gid, "name": "TEST Cooking"},
                "section": {"gid": section_gid, "name": "Queue"},
            }
        ],
    }


def test_target_uuid_is_stable_typed_uuid5_mapping() -> None:
    assert str(ASANA_IDENTITY_NAMESPACE) == "a8ad7ec4-ec82-5764-b89e-1fed9c62e4a1"
    assert str(target_uuid("task", "123")) == "e31c869f-beeb-5ec2-9393-786738ac0647"
    assert str(target_uuid("project", "123")) == "94cd945e-d34b-5a96-baa3-7044f2d377f8"
    assert str(target_uuid("section", "123")) == "da35e9a7-236d-5bb7-8160-c4a4c77f31b8"
    with pytest.raises(LocationManifestError, match="unsupported"):
        target_uuid("workspace", "123")


def test_manifest_matches_sqlite_corpus_and_exports_importer_compatible_source(tmp_path) -> None:
    database = tmp_path / "test.sqlite3"
    _database(database, "100", "200")
    project_gid = "900"
    responses = {
        "100": _task("100", project_gid, "901", completed=False),
        "200": _task("200", project_gid, "902", completed=True),
    }
    observations = iter(
        (
            datetime(2026, 8, 3, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 3, 9, 2, tzinfo=timezone.utc),
        )
    )

    manifest_value = build_location_manifest(
        database=database,
        project_gid=project_gid,
        read_task=responses.__getitem__,
        now=lambda: next(observations),
    )

    assert list(manifest_value) == ["tasks"]
    assert list(manifest_value["tasks"]) == ["100", "200"]
    first = manifest_value["tasks"]["100"]
    assert first == {
        "task_id": str(target_uuid("task", "100")),
        "project_ids": [str(target_uuid("project", project_gid))],
        "section_id": str(target_uuid("section", "901")),
        "completed": False,
        "observed_at": "2026-08-03T09:01:00+00:00",
        "existence_state": "ordinary",
    }

    manifest = tmp_path / "locations.json"
    _atomic_json(manifest, manifest_value)
    assert manifest.stat().st_mode & 0o777 == 0o600
    source = tmp_path / "source.ndjson"
    assert export_legacy_source(
        database=database, location_manifest=manifest, output=source
    ) == 2

    records = list(iter_source(source))
    assert [record.error for record in records] == [None, None]
    assert [record.spec.task_id for record in records] == [
        target_uuid("task", "100"),
        target_uuid("task", "200"),
    ]
    assert records[0].spec.project_ids == (target_uuid("project", project_gid),)
    assert records[1].spec.section_id == target_uuid("section", "902")
    assert records[1].spec.completed is True


def test_manifest_fails_when_sqlite_task_is_not_in_test_project(tmp_path) -> None:
    database = tmp_path / "test.sqlite3"
    _database(database, "100")
    response = _task("100", "999", "901", completed=False)

    with pytest.raises(LocationManifestError, match="not a member of TEST project 900"):
        build_location_manifest(
            database=database,
            project_gid="900",
            read_task=lambda _task_gid: response,
        )


def test_manifest_fails_closed_on_ambiguous_test_project_placement(tmp_path) -> None:
    database = tmp_path / "test.sqlite3"
    _database(database, "100")
    response = _task("100", "900", "901", completed=False)
    response["memberships"].append(
        {"project": {"gid": "900"}, "section": {"gid": "902"}}
    )

    with pytest.raises(LocationManifestError, match="ambiguous placement"):
        build_location_manifest(
            database=database,
            project_gid="900",
            read_task=lambda _task_gid: response,
        )


def test_environment_file_requires_owner_only_permissions(tmp_path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text(
        'DISH_DB_PATH="/home/marco/.local/state/dish/test/shared.sqlite3"\n'
        "DISH_COOKING_PROJECT_GID=1216693403164366\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)
    with pytest.raises(LocationManifestError, match="mode 0600"):
        _environment_file(env_file)

    env_file.chmod(0o600)
    assert _environment_file(env_file)["DISH_COOKING_PROJECT_GID"] == "1216693403164366"


def test_test_configuration_rejects_project_or_database_escape(tmp_path, monkeypatch) -> None:
    state_root = tmp_path / "state" / "test"
    state_root.mkdir(parents=True)
    database = state_root / "shared.sqlite3"
    database.touch()
    env_file = tmp_path / "test.env"

    def write_env(project_gid: str, database_path: Path) -> None:
        env_file.write_text(
            f"DISH_COOKING_PROJECT_GID={project_gid}\n"
            f"DISH_DB_PATH={database_path}\n"
            "ASANA_PAT=test-token\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)

    monkeypatch.setattr(manifest_module, "TEST_ENV_FILE", env_file)
    monkeypatch.setattr(manifest_module, "TEST_STATE_ROOT", state_root)
    monkeypatch.setattr(manifest_module, "TEST_COOKING_PROJECT_GID", "900")

    write_env("900", database)
    assert manifest_module.load_test_configuration(env_file)[:2] == (database.resolve(), "900")

    write_env("901", database)
    with pytest.raises(LocationManifestError, match="fixed TEST Cooking project"):
        manifest_module.load_test_configuration(env_file)

    outside = tmp_path / "prod" / "shared.sqlite3"
    outside.parent.mkdir()
    outside.touch()
    write_env("900", outside)
    with pytest.raises(LocationManifestError, match="must remain under"):
        manifest_module.load_test_configuration(env_file)
