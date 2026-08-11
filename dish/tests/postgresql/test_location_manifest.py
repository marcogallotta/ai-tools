from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
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
    build_location_manifest,
    target_uuid,
)


def _database(
    path: Path, *task_gids: str, last_known_sections: dict[str, str] | None = None
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE task_content_state(
        task_gid TEXT PRIMARY KEY,last_confirmed_identity TEXT,last_confirmed_title TEXT,
        last_confirmed_notes TEXT,schema_version TEXT,confirmed_at TEXT)"""
    )
    connection.execute(
        """CREATE TABLE operations(
        operation_id TEXT PRIMARY KEY, task_gid TEXT NOT NULL, operation_kind TEXT, status TEXT,
        created_at TEXT, completed_at TEXT, phase TEXT, terminal_outcome TEXT)"""
    )
    connection.execute(
        """CREATE TABLE verification_cycles(
        cycle_id TEXT PRIMARY KEY, operation_id TEXT, task_gid TEXT, cycle_number INTEGER,
        outcome TEXT, created_at TEXT, completed_at TEXT)"""
    )
    connection.execute(
        """CREATE TABLE service_leases(
        lease_id TEXT PRIMARY KEY, operation_id TEXT, task_gid TEXT, owner_id TEXT, run_id TEXT,
        acquired_at TEXT, expires_at TEXT, released_at TEXT, lease_kind TEXT,
        actor_attempt_seq INTEGER, context_cycle_id TEXT)"""
    )
    connection.execute(
        """CREATE TABLE operation_run_revocations(
        revocation_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        run_id TEXT NOT NULL, source_lease_id TEXT, reason TEXT NOT NULL, revoked_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE movement_attempts(
        attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
        outcome TEXT NOT NULL, confirmed_section_gid TEXT, finished_at TEXT)"""
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
    for index, (task_gid, section_gid) in enumerate((last_known_sections or {}).items(), 1):
        operation_id = f"op-{index}"
        connection.execute(
            "INSERT INTO operations(operation_id,task_gid) VALUES (?,?)", (operation_id, task_gid)
        )
        connection.execute(
            "INSERT INTO movement_attempts VALUES (?,?,?,?,?)",
            (
                f"mv-{index}",
                operation_id,
                "confirmed",
                section_gid,
                "2026-08-01T00:00:00+00:00",
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
                "project": {"gid": project_gid, "name": "Cooking"},
                "section": {"gid": section_gid, "name": "Queue"},
            }
        ],
    }


def _write_env(path: Path, values: dict[str, str], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(mode)


def _production_fixture(tmp_path: Path, monkeypatch, *, task_gids=("100", "200")):
    state_root = tmp_path / "state" / "prod"
    state_root.mkdir(parents=True)
    database = state_root / "shared.sqlite3"
    _database(database, *task_gids)
    env_file = tmp_path / "config" / "dish-service" / "prod.env"
    credential_file = tmp_path / "config" / "asana-cli" / ".env"
    _write_env(credential_file, {"ASANA_PAT": "read-secret"})
    project_gid = "900"
    values = {
        "DISH_COOKING_PROJECT_GID": project_gid,
        "DISH_DB_PATH": str(database),
        "DISH_SERVICE_BACKUP_DIR": str(state_root / "backups"),
        "DISH_SERVICE_PORT": "8775",
        "DISH_ACTION_PORT": "8776",
        "ASANA_ENV": str(credential_file),
        "DISH_DARK_LAUNCH_SPOOL_PATH": str(state_root / "dark-launch-spool.sqlite3"),
        "DISH_DARK_LAUNCH_EMERGENCY_DIR": str(state_root / "dark-launch-emergency"),
        "DISH_DARK_LAUNCH_KILL_SWITCH": str(state_root / "dark-launch.disabled"),
    }
    _write_env(env_file, values)
    monkeypatch.setattr(manifest_module, "PRODUCTION_ENV_FILE", env_file)
    monkeypatch.setattr(manifest_module, "PRODUCTION_STATE_ROOT", state_root)
    monkeypatch.setattr(manifest_module, "PRODUCTION_COOKING_PROJECT_GID", project_gid)
    monkeypatch.setattr(manifest_module, "PRODUCTION_ASANA_ENV_FILE", credential_file)
    return env_file, database, credential_file, project_gid, values


def test_target_uuid_is_stable_typed_uuid5_mapping() -> None:
    assert str(ASANA_IDENTITY_NAMESPACE) == "a8ad7ec4-ec82-5764-b89e-1fed9c62e4a1"
    assert str(target_uuid("task", "123")) == "e31c869f-beeb-5ec2-9393-786738ac0647"
    assert str(target_uuid("project", "123")) == "94cd945e-d34b-5a96-baa3-7044f2d377f8"
    assert str(target_uuid("section", "123")) == "da35e9a7-236d-5bb7-8160-c4a4c77f31b8"
    with pytest.raises(LocationManifestError, match="unsupported"):
        target_uuid("workspace", "123")


def test_manifest_matches_sqlite_corpus_and_exports_importer_compatible_source(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "test.sqlite3"
    _database(database, "100", "200")
    project_gid = "900"
    responses = {
        "100": _task("100", project_gid, "901", completed=False),
        "200": _task("200", project_gid, "902", completed=True),
    }
    calls: list[str] = []
    observations = iter(
        (
            datetime(2026, 8, 3, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 3, 9, 2, tzinfo=timezone.utc),
        )
    )
    original_connect = sqlite3.connect
    sqlite_calls = []

    def connect(*args, **kwargs):
        sqlite_calls.append((args, kwargs))
        return original_connect(*args, **kwargs)

    def read_task(task_gid: str) -> dict:
        calls.append(task_gid)
        return responses[task_gid]

    monkeypatch.setattr(manifest_module.sqlite3, "connect", connect)
    value = build_location_manifest(
        database=database,
        project_gid=project_gid,
        read_task=read_task,
        now=lambda: next(observations),
    )
    assert calls == ["100", "200"]
    assert sqlite_calls == [((f"file:{database.resolve()}?mode=ro",), {"uri": True})]
    assert list(value) == ["tasks"]
    assert list(value["tasks"]) == ["100", "200"]
    assert value["tasks"]["100"] == {
        "task_id": str(target_uuid("task", "100")),
        "project_ids": [str(target_uuid("project", project_gid))],
        "section_id": str(target_uuid("section", "901")),
        "section_gid": "901",
        "section_name": "Queue",
        "completed": False,
        "observed_at": "2026-08-03T09:01:00+00:00",
        "existence_state": "ordinary",
    }
    manifest = tmp_path / "locations.json"
    _atomic_json(manifest, value)
    source = tmp_path / "source.ndjson"
    assert export_legacy_source(database=database, location_manifest=manifest, output=source) == 2
    records = list(iter_source(source))
    assert [record.error for record in records] == [None, None]
    assert [record.spec.task_id for record in records] == [
        target_uuid("task", "100"),
        target_uuid("task", "200"),
    ]
    assert records[0].spec.project_ids == (target_uuid("project", project_gid),)
    assert records[1].spec.section_id == target_uuid("section", "902")
    assert records[1].spec.completed is True
    exported = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    assert [record["section_name"] for record in exported] == ["Queue", "Queue"]

@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda task: task.update(gid="101"), "identity mismatch"),
        (lambda task: task.update(completed="false"), "must be a boolean"),
        (lambda task: task.update(memberships="bad"), "malformed task memberships"),
        (lambda task: task["memberships"].clear(), "no last known section"),
        (lambda task: task["memberships"][0]["section"].update(name=""), "section.name"),
        (
            lambda task: task["memberships"].append(
                {"project": {"gid": "900"}, "section": {"gid": "902"}}
            ),
            "ambiguous placement",
        ),
    ],
)
def test_manifest_fails_closed_on_malformed_or_inexact_task(
    tmp_path: Path, mutate, message: str
) -> None:
    database = tmp_path / "test.sqlite3"
    _database(database, "100")
    response = _task("100", "900", "901", completed=False)
    mutate(response)
    with pytest.raises(LocationManifestError, match=message):
        build_location_manifest(
            database=database,
            project_gid="900",
            read_task=lambda _task_gid: response,
            environment="production",
        )


def test_manifest_omits_departed_task_when_operator_allows_it(
    tmp_path: Path,
) -> None:
    """The explicit production opt-in permanently excludes a departed task."""
    database = tmp_path / "test.sqlite3"
    _database(database, "100", last_known_sections={"100": "901"})
    project_gid = "900"
    departed = _task("100", project_gid, "901", completed=True)
    departed["memberships"].clear()
    value = build_location_manifest(
        database=database,
        project_gid=project_gid,
        read_task=lambda _gid: departed,
        environment="production",
        allow_departed_tasks=True,
    )
    assert "100" not in value["tasks"]


def test_test_manifest_preserves_project_membership_failures(tmp_path: Path) -> None:
    database = tmp_path / "test.sqlite3"
    _database(database, "100")
    missing = _task("100", "999", "901", completed=False)
    ambiguous = _task("100", "900", "901", completed=False)
    ambiguous["memberships"].append({"project": {"gid": "900"}, "section": {"gid": "902"}})
    for response, message in ((missing, "no last known section"), (ambiguous, "ambiguous placement in TEST project 900")):
        with pytest.raises(LocationManifestError, match=message):
            build_location_manifest(database=database, project_gid="900", read_task=lambda _gid: response)


def test_production_manifest_requires_nonzero_sqlite_corpus(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite3"
    _database(database)
    with pytest.raises(LocationManifestError, match="must be non-zero"):
        build_location_manifest(
            database=database,
            project_gid="900",
            read_task=lambda _task_gid: pytest.fail("empty corpus must not read Asana"),
            environment="production",
            require_nonzero=True,
        )


def test_test_configuration_preserves_fixed_test_identity(tmp_path: Path, monkeypatch) -> None:
    state_root = tmp_path / "state" / "test"
    state_root.mkdir(parents=True)
    database = state_root / "shared.sqlite3"
    database.touch()
    env_file = tmp_path / "test.env"
    monkeypatch.setattr(manifest_module, "TEST_ENV_FILE", env_file)
    monkeypatch.setattr(manifest_module, "TEST_STATE_ROOT", state_root)
    monkeypatch.setattr(manifest_module, "TEST_COOKING_PROJECT_GID", "900")

    _write_env(
        env_file,
        {"DISH_COOKING_PROJECT_GID": "900", "DISH_DB_PATH": str(database), "ASANA_PAT": "x"},
    )
    assert manifest_module.load_test_configuration(env_file)[:2] == (database.resolve(), "900")
    _write_env(
        env_file,
        {"DISH_COOKING_PROJECT_GID": "901", "DISH_DB_PATH": str(database), "ASANA_PAT": "x"},
    )
    with pytest.raises(LocationManifestError, match="fixed TEST Cooking project"):
        manifest_module.load_test_configuration(env_file)

    outside = tmp_path / "prod" / "shared.sqlite3"
    outside.parent.mkdir()
    outside.touch()
    _write_env(
        env_file,
        {"DISH_COOKING_PROJECT_GID": "900", "DISH_DB_PATH": str(outside), "ASANA_PAT": "x"},
    )
    with pytest.raises(LocationManifestError, match="must remain under"):
        manifest_module.load_test_configuration(env_file)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("DISH_COOKING_PROJECT_GID", "901"),
        ("DISH_SERVICE_BACKUP_DIR", "{test_root}/backups"),
        ("DISH_SERVICE_PORT", "8765"),
        ("DISH_ACTION_PORT", "8766"),
        ("DISH_DARK_LAUNCH_SPOOL_PATH", "{test_root}/dark-launch-spool.sqlite3"),
        ("DISH_DARK_LAUNCH_EMERGENCY_DIR", "{test_root}/dark-launch-emergency"),
        ("DISH_DARK_LAUNCH_KILL_SWITCH", "{test_root}/dark-launch.disabled"),
        ("DISH_PROFILE", "test"),
    ],
)
def test_production_configuration_rejects_mixed_test_identity(
    tmp_path: Path, monkeypatch, field: str, replacement: str
) -> None:
    env_file, _database_path, _credential, _project, values = _production_fixture(
        tmp_path, monkeypatch
    )
    test_root = tmp_path / "state" / "test"
    values[field] = replacement.format(test_root=test_root)
    _write_env(env_file, values)
    with pytest.raises(LocationManifestError, match="production"):
        manifest_module.load_production_configuration(env_file)


def test_production_configuration_rejects_test_database_and_alternate_env_file(
    tmp_path: Path, monkeypatch
) -> None:
    env_file, _database_path, _credential, _project, values = _production_fixture(
        tmp_path, monkeypatch
    )
    test_database = tmp_path / "state" / "test" / "shared.sqlite3"
    test_database.parent.mkdir(parents=True)
    test_database.touch()
    values["DISH_DB_PATH"] = str(test_database)
    _write_env(env_file, values)
    with pytest.raises(LocationManifestError, match="production database must be"):
        manifest_module.load_production_configuration(env_file)

    alternate = tmp_path / "alternate-prod.env"
    _write_env(alternate, values)
    with pytest.raises(LocationManifestError, match="environment file must be"):
        manifest_module.load_production_configuration(alternate)


def test_production_configuration_rejects_hardlink_aliases_to_test_authority(
    tmp_path: Path, monkeypatch
) -> None:
    env_file, database, _credential, _project, _values = _production_fixture(
        tmp_path, monkeypatch
    )
    test_env = tmp_path / "config" / "dish-service" / "test.env"
    os.link(env_file, test_env)
    monkeypatch.setattr(manifest_module, "TEST_ENV_FILE", test_env)
    with pytest.raises(LocationManifestError, match="alias the TEST environment"):
        manifest_module.load_production_configuration(env_file)

    test_env.unlink()
    test_root = tmp_path / "state" / "test"
    test_root.mkdir(parents=True)
    os.link(database, test_root / "shared.sqlite3")
    monkeypatch.setattr(manifest_module, "TEST_STATE_ROOT", test_root)
    with pytest.raises(LocationManifestError, match="alias the TEST database"):
        manifest_module.load_production_configuration(env_file)


def test_production_configuration_rejects_ambiguous_or_unsafe_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    env_file, _database_path, credential, _project, values = _production_fixture(
        tmp_path, monkeypatch
    )
    values.pop("ASANA_ENV")
    _write_env(env_file, values)
    with pytest.raises(LocationManifestError, match="missing ASANA_ENV"):
        manifest_module.load_production_configuration(env_file)

    values["ASANA_ENV"] = str(credential)
    values["ASANA_PAT"] = "inline"
    _write_env(env_file, values)
    with pytest.raises(LocationManifestError, match="fixed Asana credential environment"):
        manifest_module.load_production_configuration(env_file)

    values.pop("ASANA_PAT")
    _write_env(env_file, values)
    credential.write_text("OTHER=value\n", encoding="utf-8")
    with pytest.raises(LocationManifestError, match="missing ASANA_PAT"):
        manifest_module.load_production_configuration(env_file)
    _write_env(credential, {"ASANA_PAT": "read-secret"}, mode=0o644)
    _write_env(env_file, values)
    with pytest.raises(LocationManifestError, match="mode 0600"):
        manifest_module.load_production_configuration(env_file)

    credential.chmod(0o600)
    alternate_credential = tmp_path / "other.env"
    _write_env(alternate_credential, {"ASANA_PAT": "x"})
    values["ASANA_ENV"] = str(alternate_credential)
    _write_env(env_file, values)
    with pytest.raises(LocationManifestError, match="credential environment must be"):
        manifest_module.load_production_configuration(env_file)


def test_production_capture_reads_exact_corpus_restores_environment_and_exports(
    tmp_path: Path, monkeypatch
) -> None:
    env_file, database, _credential, project_gid, _values = _production_fixture(
        tmp_path, monkeypatch
    )
    responses = {
        "100": _task("100", project_gid, "901", completed=False),
        "200": _task("200", project_gid, "902", completed=True),
    }
    calls: list[str] = []
    sqlite_calls = []
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        sqlite_calls.append((args, kwargs))
        return original_connect(*args, **kwargs)

    @contextmanager
    def reader():
        assert os.environ["ASANA_PAT"] == "read-secret"
        assert "ASANA_ENV" not in os.environ

        def read_task(task_gid: str) -> dict:
            calls.append(task_gid)
            return responses[task_gid]

        assert not any(
            hasattr(read_task, name)
            for name in ("create_task", "update_task", "delete_task", "move_task_to_section")
        )
        yield read_task

    monkeypatch.setattr(manifest_module, "production_task_reader", reader)
    monkeypatch.setattr(manifest_module.sqlite3, "connect", connect)
    monkeypatch.setenv("ASANA_PAT", "caller-pat")
    monkeypatch.setenv("ASANA_ENV", "caller-env")
    output = tmp_path / "locations.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o644)

    assert manifest_module.capture_production_location_manifest(
        env_file=env_file, output=output
    ) == 2
    assert calls == ["100", "200"]
    assert sqlite_calls == [((f"file:{database.resolve()}?mode=ro",), {"uri": True})]
    assert os.environ["ASANA_PAT"] == "caller-pat"
    assert os.environ["ASANA_ENV"] == "caller-env"
    assert output.stat().st_mode & 0o777 == 0o600
    value = json.loads(output.read_text(encoding="utf-8"))
    assert set(value) == {"tasks"}
    assert set(value["tasks"]) == {"100", "200"}
    monkeypatch.setattr(manifest_module.sqlite3, "connect", original_connect)
    source = tmp_path / "source.ndjson"
    assert export_legacy_source(database=database, location_manifest=output, output=source) == 2


def test_production_capture_redacts_backend_error_and_restores_environment(
    tmp_path: Path, monkeypatch
) -> None:
    env_file, _database_path, _credential, _project, _values = _production_fixture(
        tmp_path, monkeypatch, task_gids=("100",)
    )
    secret = "credential-must-not-leak"

    @contextmanager
    def failing_reader():
        def read_task(_task_gid: str):
            raise RuntimeError(secret)

        yield read_task

    monkeypatch.setattr(manifest_module, "production_task_reader", failing_reader)
    monkeypatch.setenv("ASANA_PAT", "caller-pat")
    monkeypatch.delenv("ASANA_ENV", raising=False)
    with pytest.raises(LocationManifestError) as error:
        manifest_module.capture_production_location_manifest(
            env_file=env_file, output=tmp_path / "locations.json"
        )
    assert secret not in str(error.value)
    assert os.environ["ASANA_PAT"] == "caller-pat"
    assert "ASANA_ENV" not in os.environ


def test_production_reader_uses_only_generated_get_task(monkeypatch) -> None:
    asana = pytest.importorskip("asana")
    calls = []

    def call_api(self, resource_path, http_method, *args, **kwargs):
        calls.append((resource_path, http_method, args, kwargs))
        return {"data": _task("100", "900", "901", completed=False)}

    monkeypatch.setenv("ASANA_PAT", "read-only-token")
    monkeypatch.delenv("ASANA_ENV", raising=False)
    monkeypatch.setattr(asana.ApiClient, "call_api", call_api)
    with manifest_module.production_task_reader() as read_task:
        assert read_task("100")["gid"] == "100"
        assert not any(
            hasattr(read_task, name)
            for name in ("create_task", "update_task", "delete_task", "add_task_for_section")
        )
    assert [(path, method) for path, method, _args, _kwargs in calls] == [
        ("/tasks/{task_gid}", "GET")
    ]


def test_cli_requires_explicit_production_mode_and_defaults_to_test(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        manifest_module,
        "capture_test_location_manifest",
        lambda **kwargs: calls.append(("test", kwargs)) or 1,
    )
    monkeypatch.setattr(
        manifest_module,
        "capture_production_location_manifest",
        lambda **kwargs: calls.append(("production", kwargs)) or 2,
    )
    output = tmp_path / "locations.json"
    assert manifest_module.main(["--output", str(output)]) == 0
    assert manifest_module.main(
        ["--environment", "production", "--output", str(output)]
    ) == 0
    assert [item[0] for item in calls] == ["test", "production"]
    assert calls[0][1]["env_file"] == manifest_module.TEST_ENV_FILE
    assert calls[1][1]["env_file"] == manifest_module.PRODUCTION_ENV_FILE
