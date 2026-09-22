from __future__ import annotations

import hashlib
import http.client
import io
import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer
from dish_tool import prod_release
from dish_tool.code_identity import executable_code_release
from dish_tool.prod_release import (
    DEFAULT_ENV_FILES,
    MANIFEST_NAME,
    Release,
    ReleaseError,
    SystemOperations,
    _installed_packages_sha256,
    _source_tree_sha256,
    activate_release,
    certify_existing_release,
    load_release,
    preflight_database,
)


def _write_release(root: Path, identity: str, schema: str = "0054_test") -> Release:
    release = root / identity
    (release / "dish/dish_pg").mkdir(parents=True)
    (release / "dish/.venv/bin").mkdir(parents=True)
    files = {
        "dish/dish-service": "#!/bin/sh\n",
        "dish/dish_pg/schema_identity.py": f'ALEMBIC_HEAD = "{schema}"\n',
        "dish/dish_service/runtime.py": "RUNTIME = True\n",
        "dish/requirements.txt": "example==1\n",
    }
    hashes: dict[str, str] = {}
    for name, content in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        hashes[name] = hashlib.sha256(content.encode()).hexdigest()
    python = release / "dish/.venv/bin/python"
    python.write_text("#!/bin/sh\nprintf 'example==1\\n'\n", encoding="utf-8")
    python.chmod(0o755)
    (release / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "source_commit": identity,
                "schema_head": schema,
                "created_at": "2026-09-21T00:00:00Z",
                "hashes": hashes,
                "source_tree_sha256": _source_tree_sha256(release),
                "installed_packages_sha256": _installed_packages_sha256(python),
            }
        ),
        encoding="utf-8",
    )
    return load_release(root, identity)


class _Operations:
    def __init__(self, *, fail_target: str | None = None) -> None:
        self.fail_target = fail_target
        self.restarts = 0
        self.verified: list[str] = []

    def restart(self) -> None:
        self.restarts += 1

    def verify(self, release: Release) -> None:
        self.verified.append(release.source_commit)
        if release.source_commit == self.fail_target:
            raise ReleaseError("simulated failed health")


def test_load_release_rejects_mislabeled_and_modified_artifacts(tmp_path: Path) -> None:
    identity = "a" * 40
    release = _write_release(tmp_path, identity)
    assert release.source_commit == identity

    (release.root / "dish/dish-service").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="integrity check failed"):
        load_release(tmp_path, identity)

    with pytest.raises(ReleaseError, match="exact lowercase commit"):
        load_release(tmp_path, "not-a-commit")

    other = _write_release(tmp_path, "b" * 40)
    (other.root / "dish/dish_service/runtime.py").write_text(
        "RUNTIME = False\n", encoding="utf-8"
    )
    with pytest.raises(ReleaseError, match="source-tree integrity"):
        load_release(tmp_path, other.source_commit)

    dependency_changed = _write_release(tmp_path, "c" * 40)
    python = dependency_changed.root / "dish/.venv/bin/python"
    python.write_text("#!/bin/sh\nprintf 'example==2\\n'\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="dependency integrity"):
        load_release(tmp_path, dependency_changed.source_commit)


def test_certify_existing_rejects_mislabeled_git_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = "a" * 40
    release = _write_release(tmp_path, identity)
    (release.root / MANIFEST_NAME).unlink()

    class _Completed:
        returncode = 0

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def mismatched(command, **kwargs):
        return _Completed("b" * 40 + "\n" if "rev-parse" in command else "")

    monkeypatch.setattr("dish_tool.prod_release.subprocess.run", mismatched)
    with pytest.raises(ReleaseError, match="HEAD does not match"):
        certify_existing_release(tmp_path, identity)

    def exact(command, **kwargs):
        return _Completed(identity + "\n" if "rev-parse" in command else "")

    monkeypatch.setattr("dish_tool.prod_release.subprocess.run", exact)
    assert certify_existing_release(tmp_path, identity).source_commit == identity


def test_activate_records_exact_previous_and_switches_atomically(
    tmp_path: Path,
) -> None:
    old = _write_release(tmp_path / "releases", "a" * 40)
    new = _write_release(tmp_path / "releases", "b" * 40)
    current = tmp_path / "prod-current"
    previous = tmp_path / "prod-previous"
    current.symlink_to(old.root)
    operations = _Operations()

    activate_release(
        new,
        current=current,
        previous=previous,
        operations=operations,  # type: ignore[arg-type]
        database_preflight=lambda release: None,
    )

    assert current.resolve() == new.root.resolve()
    assert previous.resolve() == old.root.resolve()
    assert operations.restarts == 1
    assert operations.verified == [new.source_commit]


def test_failed_activation_restores_both_exact_pointers_and_prior_service(
    tmp_path: Path,
) -> None:
    releases = tmp_path / "releases"
    older = _write_release(releases, "0" * 40)
    old = _write_release(releases, "a" * 40)
    new = _write_release(releases, "b" * 40)
    current = tmp_path / "prod-current"
    previous = tmp_path / "prod-previous"
    current.symlink_to(old.root)
    previous.symlink_to(older.root)
    operations = _Operations(fail_target=new.source_commit)

    with pytest.raises(ReleaseError, match="prior pointer was restored"):
        activate_release(
            new,
            current=current,
            previous=previous,
            operations=operations,  # type: ignore[arg-type]
            database_preflight=lambda release: None,
        )

    assert current.resolve() == old.root.resolve()
    assert previous.resolve() == older.root.resolve()
    assert operations.restarts == 2
    assert operations.verified == [new.source_commit, old.source_commit]


def test_schema_mismatch_refuses_before_any_restart(tmp_path: Path) -> None:
    release = _write_release(tmp_path / "releases", "a" * 40, schema="0054_expected")
    env = tmp_path / "prod.env"
    env.write_text(
        "DISH_PG_EXPECTED_SCHEMA_HEAD=0045_old\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseError, match="incompatible with configured schema"):
        preflight_database(release, env)


def test_preflight_uses_candidate_runtime_to_check_live_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = _write_release(tmp_path / "releases", "a" * 40)
    env = tmp_path / "prod.env"
    env.write_text(
        "DISH_PG_EXPECTED_SCHEMA_HEAD=0054_test\n"
        "DISH_PG_DATABASE_URL=postgresql://dish:secret@localhost/dish_prod\n"
        "DISH_PG_EXPECTED_DATABASE_NAME=dish_prod\n"
        "DISH_PG_EXPECTED_RELEASE=dish@generation\n"
        "DISH_PG_EXPECTED_GENERATION_ID=11111111-1111-4111-8111-111111111111\n",
        encoding="utf-8",
    )
    observed: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        observed.append(command)
        return _Completed()

    monkeypatch.setattr("dish_tool.prod_release.subprocess.run", fake_run)
    preflight_database(release, env)

    assert observed == [
        [
            str(release.dish_root / ".venv/bin/python"),
            "-m",
            "dish_pg.release_preflight",
        ]
    ]


def test_preflight_combines_canonical_service_environment_files_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = _write_release(tmp_path / "releases", "a" * 40)
    service_env = tmp_path / "prod.env"
    postgres_env = tmp_path / "postgres-prod.env"
    service_env.write_text(
        "DISH_PG_EXPECTED_SCHEMA_HEAD=overridden-by-postgres-env\n"
        "DISH_AGENT_TOKEN=service-token\n",
        encoding="utf-8",
    )
    postgres_env.write_text(
        "DISH_PG_EXPECTED_SCHEMA_HEAD=0054_test\n"
        "DISH_PG_DATABASE_URL=postgresql://dish:secret@localhost/dish_prod\n"
        "DISH_PG_EXPECTED_DATABASE_NAME=dish_prod\n"
        "DISH_PG_EXPECTED_RELEASE=dish@generation\n"
        "DISH_PG_EXPECTED_GENERATION_ID=11111111-1111-4111-8111-111111111111\n",
        encoding="utf-8",
    )

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        assert kwargs["env"]["DISH_AGENT_TOKEN"] == "service-token"
        assert kwargs["env"]["DISH_PG_EXPECTED_SCHEMA_HEAD"] == "0054_test"
        assert kwargs["env"]["DISH_PG_EXPECTED_DATABASE_NAME"] == "dish_prod"
        return _Completed()

    monkeypatch.setattr("dish_tool.prod_release.subprocess.run", fake_run)
    preflight_database(release, (service_env, postgres_env))


@pytest.mark.parametrize("command", ["activate", "rollback"])
def test_documented_commands_use_both_canonical_environment_files_before_mutation(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = _write_release(tmp_path / "releases", "a" * 40)
    current = tmp_path / "prod-current"
    previous = tmp_path / "prod-previous"
    if command == "rollback":
        previous.symlink_to(release.root)
    observed: list[tuple[Path, ...]] = []
    events: list[str] = []

    def fake_preflight(candidate: Release, env_files: tuple[Path, ...]) -> None:
        assert candidate == release
        observed.append(env_files)
        events.append("preflight")

    class _RecordingOperations:
        def restart(self) -> None:
            events.append("restart")

        def verify(self, candidate: Release) -> None:
            assert candidate == release
            events.append("verify")

    monkeypatch.setattr(prod_release, "preflight_database", fake_preflight)
    monkeypatch.setattr(
        prod_release,
        "SystemOperations",
        lambda *args, **kwargs: _RecordingOperations(),
    )
    argv = [
        "--release-root",
        str(release.root.parent),
        "--current",
        str(current),
        "--previous",
        str(previous),
        command,
    ]
    if command == "activate":
        argv.append(release.source_commit)

    assert prod_release.main(argv) == 0
    assert observed == [DEFAULT_ENV_FILES]
    assert events == ["preflight", "restart", "verify"]


def test_executable_identity_is_distinct_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / MANIFEST_NAME
    identity = "c" * 40
    manifest.write_text(json.dumps({"source_commit": identity}), encoding="utf-8")
    monkeypatch.setenv("DISH_RELEASE_MANIFEST", str(manifest))
    assert executable_code_release() == identity

    manifest.write_text(
        json.dumps({"source_commit": "dish@database-generation"}), encoding="utf-8"
    )
    assert executable_code_release() is None


def test_system_verification_binds_health_identity_to_real_process_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = _write_release(tmp_path / "releases", "d" * 40)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"], cwd=release.dish_root
    )

    class _Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if "is-active" in command:
            return _Completed("active\n")
        if "show" in command:
            return _Completed(f"{process.pid}\n")
        raise AssertionError(command)

    payload = json.dumps({"ok": True, "code_release": release.source_commit}).encode()
    monkeypatch.setattr("dish_tool.prod_release.subprocess.run", fake_run)
    monkeypatch.setattr(
        "dish_tool.prod_release.urllib.request.urlopen",
        lambda *args, **kwargs: io.BytesIO(payload),
    )
    try:
        SystemOperations("dish-service-prod.service", "http://health", 1).verify(
            release
        )
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_private_health_exposes_executable_identity_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = "e" * 40
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(json.dumps({"source_commit": identity}), encoding="utf-8")
    monkeypatch.setenv("DISH_RELEASE_MANIFEST", str(manifest))

    @dataclass
    class _Service:
        config: ServiceConfig

        def health(self):
            return {
                "ok": True,
                "identity": {"dish_release": "dish@database-generation"},
            }

    config = ServiceConfig(
        db_path=tmp_path / "dish.db",
        honest_root=tmp_path,
        port=0,
        action_port=0,
        agent_token="agent-secret-123",
        admin_token="admin-secret-456",
        action_token="action-secret-789",
    )
    server = DishHTTPServer(
        ("127.0.0.1", 0), cast(Any, _Service(config)), surface_mode="private"
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert payload["code_release"] == identity
    assert payload["identity"]["dish_release"] == "dish@database-generation"


def test_production_unit_runs_only_through_the_release_pointer() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/dish-service-prod.service").read_text(
        encoding="utf-8"
    )
    assert "WorkingDirectory=/home/marco/.local/share/dish/prod-current/dish" in unit
    assert (
        "ExecStart=/home/marco/.local/share/dish/prod-current/dish/dish-service" in unit
    )
    assert (
        "DISH_RELEASE_MANIFEST=/home/marco/.local/share/dish/prod-current/.dish-release.json"
        in unit
    )
    assert "RestartPreventExitStatus=78" in unit
    assert "/home/marco/ai-tools/dish" not in unit


def test_production_unit_is_compatible_with_the_user_systemd_manager() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/dish-service-prod.service").read_text(
        encoding="utf-8"
    )
    assert "WantedBy=default.target" in unit
    assert "User=" not in unit
    assert "PrivateDevices=" not in unit
    assert "ProtectKernelModules=" not in unit
    assert "tailscaled.service" not in unit


def test_all_shipped_dish_units_are_compatible_with_the_user_systemd_manager() -> None:
    root = Path(__file__).resolve().parents[1]
    systemd_root = root / "deploy/systemd"
    service_units = sorted(systemd_root.glob("dish-*.service"))

    assert service_units
    for path in service_units:
        unit = path.read_text(encoding="utf-8")
        assert "User=" not in unit, path.name
        assert "PrivateDevices=" not in unit, path.name
        assert "ProtectKernelModules=" not in unit, path.name
        assert "AmbientCapabilities=" not in unit, path.name
        assert "CapabilityBoundingSet=" not in unit, path.name
        assert "tailscaled.service" not in unit, path.name
        assert "docker.service" not in unit, path.name
        assert "WantedBy=multi-user.target" not in unit, path.name

    frontend_target = (systemd_root / "dish-frontend.target").read_text(encoding="utf-8")
    assert "WantedBy=default.target" in frontend_target
    assert "WantedBy=multi-user.target" not in frontend_target

    backup = (systemd_root / "dish-postgres-backup.service").read_text(encoding="utf-8")
    assert "ExecStartPre=+" not in backup

    frontend_test_caddy = (root / "deploy/caddy/dish-frontend-test.Caddyfile").read_text(
        encoding="utf-8"
    )
    assert "https://{$DISH_FRONTEND_TEST_HOST}:8443" in frontend_test_caddy
