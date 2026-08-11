from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from dish_pg.bootstrap import DEFAULT_SCHEMA_HEAD
from dish_pg.dark_launch_readiness import PreflightInputs
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.core import HASH_A, HASH_B

ROOT = Path(__file__).resolve().parents[3]
UNIT = ROOT / "deploy/systemd/dish-shadow-worker.service"
ENV_EXAMPLE = ROOT / "deploy/systemd/dark-launch.env.example"


def example_assignments() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            raise AssertionError(f"invalid environment example line: {raw!r}")
        values[name] = value
    return values


def valid_worker_values(tmp_path: Path) -> dict[str, str]:
    values = example_assignments()
    values.update(
        {
            "DISH_PG_DATABASE_URL": "postgresql+psycopg://dish:secret@127.0.0.1:5432/dish_prod",
            "DISH_PG_EXPECTED_DATABASE_NAME": "dish_prod",
            "DISH_DARK_LAUNCH_SPOOL_PATH": str(tmp_path / "spool.sqlite3"),
            "DISH_DARK_LAUNCH_BASELINE_ID": str(uuid.uuid4()),
            "DISH_DARK_LAUNCH_WORKER_ID": "prod-shadow-1",
            "DISH_PG_CURSOR_SECRET_FILE": str(tmp_path / "cursor-secret"),
            "DISH_DARK_LAUNCH_COMPARATOR_RELEASE": "dish@0123456789abcdef",
            "DISH_DARK_LAUNCH_KILL_SWITCH": str(tmp_path / "dark-launch.disabled"),
        }
    )
    return values


def write_owner_file(path: Path, body: bytes = b"fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(body)
    path.chmod(0o600)


def path_fixture(tmp_path: Path):
    state_root = tmp_path / "prod"
    config_root = tmp_path / "config"
    evidence_root = tmp_path / "evidence"
    for root in (state_root, config_root, evidence_root):
        root.mkdir(mode=0o700)
    sqlite = state_root / "shared.sqlite3"
    spool = state_root / "dark-launch-spool.sqlite3"
    emergency = state_root / "emergency"
    emergency.mkdir(mode=0o700)
    cursor = config_root / "cursor-secret"
    service_env = config_root / "prod.env"
    worker_env = config_root / "dark-launch.env"
    manifest = evidence_root / "manifest.json"
    ndjson = evidence_root / "legacy.ndjson"
    receipt = evidence_root / "receipt.json"
    for path in (sqlite, spool, cursor, service_env, worker_env, manifest, ndjson, receipt):
        write_owner_file(path)
    kill = state_root / "dark-launch.disabled"
    service_values = {
        "DISH_HONEST_PATH": str(tmp_path / "honest-pantry"),
        "DISH_DB_PATH": str(sqlite),
        "DISH_DARK_LAUNCH_EMERGENCY_DIR": str(emergency),
        "DISH_DARK_LAUNCH_SPOOL_PATH": str(spool),
        "DISH_DARK_LAUNCH_KILL_SWITCH": str(kill),
        "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS": "50",
        "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES": "536870912",
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS": "100000",
        "DISH_DARK_LAUNCH_MIN_FREE_BYTES": "1073741824",
    }
    worker_values = {
        "DISH_DARK_LAUNCH_SPOOL_PATH": str(spool),
        "DISH_DARK_LAUNCH_KILL_SWITCH": str(kill),
        "DISH_PG_CURSOR_SECRET_FILE": str(cursor),
        "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS": "50",
        "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES": "536870912",
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS": "100000",
        "DISH_DARK_LAUNCH_MIN_FREE_BYTES": "1073741824",
    }
    inputs = PreflightInputs(
        service_environment=service_env,
        worker_environment=worker_env,
        database_url="postgresql+psycopg://dish:secret@localhost/dish_prod",
        expected_database_name="dish_prod",
        manifest=manifest,
        legacy_ndjson=ndjson,
        bootstrap_receipt=receipt,
        spool_path=spool,
        kill_switch=kill,
        unit_name="dish-shadow-worker.service",
        repository_unit=UNIT,
        state_root=state_root,
        config_root=config_root,
        evidence_root=evidence_root,
        report_path=evidence_root / "report.json",
    )
    return inputs, service_values, worker_values


def record(
    task_id: uuid.UUID, project_id: uuid.UUID, section_id: uuid.UUID, section_gid: str
) -> dict[str, object]:
    return {
        "task_id": str(task_id),
        "asana_task_gid": "123456789",
        "title": "[ready] Exact imported task",
        "body": "Canonical body\n---\nStatus: ready\n",
        "identity_scheme": "legacy-sha256-v1",
        "content_identity": HASH_A,
        "project_ids": [str(project_id)],
        "section_id": str(section_id),
        "section_gid": section_gid,
        "completed": False,
        "observed_at": "2025-01-15T12:00:00+00:00",
        "existence_state": "ordinary",
        "operation_history": {"operations": [], "leases": [], "verification_cycles": [], "revocations": []},
    }


def write_environment(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)


def preflight_fixture(tmp_path: Path) -> tuple[PreflightInputs, Path, str, str]:
    inputs, service_values, worker_values = path_fixture(tmp_path)
    database_password = "database-password-not-for-reports"
    service_token = "service-token-not-for-reports"
    baseline_id = uuid.uuid4()
    generation_id = uuid.uuid4()
    import_run_id = uuid.uuid4()
    binding_id = uuid.uuid4()

    task_record = record(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "1216891250619908")
    inputs.legacy_ndjson.write_text(
        json.dumps(task_record, sort_keys=True) + "\n", encoding="utf-8"
    )
    inputs.manifest.write_text(
        json.dumps(
            {
                "tasks": {
                    "123456789": {
                        key: task_record[key]
                        for key in (
                            "task_id",
                            "project_ids",
                            "section_id",
                            "section_gid",
                            "completed",
                            "observed_at",
                            "existence_state",
                        )
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    source_sha256 = hashlib.sha256(inputs.legacy_ndjson.read_bytes()).hexdigest()
    inputs.bootstrap_receipt.write_text(
        json.dumps(
            {
                "import_run_id": str(import_run_id),
                "generation_id": str(generation_id),
                "binding_id": str(binding_id),
                "source_bundle_sha256": source_sha256,
                "source_record_count": 1,
                "dish_release": "dish@42619b9",
                "honest_release": "honest-pantry@" + "f" * 40,
                "protocol_release": "1.0.10",
                "protocol_sha256": HASH_A,
                "schema_release": "2",
                "schema_sha256": HASH_B,
                "schema_head": DEFAULT_SCHEMA_HEAD,
                "source_generation": "legacy-1",
            }
        ),
        encoding="utf-8",
    )
    for path in (inputs.legacy_ndjson, inputs.manifest, inputs.bootstrap_receipt):
        path.chmod(0o600)

    inputs.spool_path.unlink()
    ShadowSpool(inputs.spool_path).status()
    inputs.spool_path.chmod(0o600)
    cursor = Path(worker_values["DISH_PG_CURSOR_SECRET_FILE"])
    cursor.write_bytes(b"c" * 32)
    cursor.chmod(0o600)
    service_values.update(
        {
            "DISH_DARK_LAUNCH_MODE": "off",
            "DISH_SERVICE_TOKEN": service_token,
        }
    )
    database_url = (
        f"postgresql+psycopg://dish:{database_password}@127.0.0.1:5432/dish_prod"
    )
    complete_worker = valid_worker_values(tmp_path)
    complete_worker.update(
        {
            "DISH_PG_DATABASE_URL": database_url,
            "DISH_DARK_LAUNCH_SPOOL_PATH": str(inputs.spool_path),
            "DISH_DARK_LAUNCH_BASELINE_ID": str(baseline_id),
            "DISH_PG_CURSOR_SECRET_FILE": str(cursor),
            "DISH_DARK_LAUNCH_KILL_SWITCH": str(inputs.kill_switch),
        }
    )
    write_environment(inputs.service_environment, service_values)
    write_environment(inputs.worker_environment, complete_worker)
    inputs = inputs.__class__(
        **{
            **inputs.__dict__,
            "database_url": database_url,
            "expected_database_name": "dish_prod",
        }
    )
    installed = tmp_path / "installed-dish-shadow-worker.service"
    installed.write_bytes(UNIT.read_bytes())
    installed.chmod(0o644)
    return inputs, installed, database_password, service_token


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def systemctl_runner(installed: Path, environment_file: Path):
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(
                (
                    "LoadState=loaded",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "UnitFileState=disabled",
                    f"FragmentPath={installed}",
                    "Result=success",
                    f"EnvironmentFiles={environment_file} (ignore_errors=no)",
                    "Environment=",
                    "PassEnvironment=",
                    "DropInPaths=",
                )
            ),
            stderr="",
        )

    return run


def passing_database_checks() -> dict[str, dict[str, object]]:
    names = (
        "postgresql_connectivity",
        "database_identity",
        "alembic_head",
        "active_generation",
        "source_import",
        "import_binding",
        "open_baseline",
        "projection_epoch",
        "imported_corpus",
    )
    return {
        name: {"passed": True, "status": "pass", "reason": "fixture evidence"}
        for name in names
    }
