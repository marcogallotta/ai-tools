"""Read-only PostgreSQL dark-launch production-readiness preflight.

This module validates an already prepared production configuration and evidence
set.  It does not create or mutate PostgreSQL authority, the legacy spool, the
kill switch, systemd, or external services.  The only optional write is the
operator-requested JSON report artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from dish_service.config import ServiceConfig
from dish_service.path_safety import (
    PathIdentityError,
    inspect_kill_switch,
    require_distinct_paths,
)
from dish_service.shadow_spool import ShadowSpool

from . import models
from . import stage5_models as tx
from .bootstrap import DEFAULT_SCHEMA_HEAD
from .database import DatabaseSettings, create_database_engine
from .import_runtime import verify_imported_records
from .importer import SourceRecord, iter_source

REPORT_FORMAT = "dish-postgresql-dark-launch-readiness-v1"
DEFAULT_STATE_ROOT = Path("/home/marco/.local/state/dish/prod")
DEFAULT_CONFIG_ROOT = Path("/home/marco/.config/dish-service")
PRODUCTION_SERVICE_ENVIRONMENT = DEFAULT_CONFIG_ROOT / "prod.env"
DEFAULT_EVIDENCE_ROOT = Path("/home/marco/.local/state/dish/prod/dark-launch-evidence")
DEFAULT_REPOSITORY_UNIT = Path("deploy/systemd/dish-shadow-worker.service")
DEFAULT_UNIT_NAME = "dish-shadow-worker.service"
MAX_REASON_LENGTH = 600
MAX_ARTIFACT_ERRORS = 20
MAX_ENV_ASSIGNMENTS = 256
MAX_SYSTEMCTL_OUTPUT_BYTES = 128 * 1024
MAX_SYSTEMD_PATHS = 32
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXECSTART_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_POSTGRES_USERINFO = re.compile(r"(?P<prefix>postgresql(?:\+[^:]+)?://)[^/@\s]+@")
_PLACEHOLDER_MARKERS = (
    "replace-me",
    "replace-with",
    "change-me",
    "changeme",
    "placeholder",
    "example.invalid",
    "<redacted>",
)


def _forbidden_worker_name(name: str) -> bool:
    upper = name.upper()
    if upper.startswith("ASANA_"):
        return True
    credential_markers = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    if any(role in upper for role in ("SERVICE", "ADMIN", "ACTION")) and any(
        marker in upper for marker in credential_markers
    ):
        return True
    return "PROJECTION" in upper and (
        "ADAPTER" in upper
        or any(marker in upper for marker in credential_markers)
    )


_SERVICE_LIMIT_ATTRIBUTES = {
    "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS": "dark_launch_busy_timeout_ms",
    "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES": "dark_launch_max_spool_bytes",
    "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS": "dark_launch_max_spool_records",
    "DISH_DARK_LAUNCH_MIN_FREE_BYTES": "dark_launch_min_free_bytes",
}
_SERVICE_REQUIRED_PATH_NAMES = (
    "DISH_DB_PATH",
    "DISH_DARK_LAUNCH_SPOOL_PATH",
    "DISH_DARK_LAUNCH_EMERGENCY_DIR",
    "DISH_DARK_LAUNCH_KILL_SWITCH",
)
_NUMERIC_ENV_NAMES = (
    "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS",
    "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES",
    "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS",
    "DISH_DARK_LAUNCH_MIN_FREE_BYTES",
    "DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS",
    "DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS",
)
_REQUIRED_RECEIPT_FIELDS = frozenset(
    {
        "import_run_id",
        "generation_id",
        "binding_id",
        "source_bundle_sha256",
        "source_record_count",
        "dish_release",
        "honest_release",
        "protocol_release",
        "protocol_sha256",
        "schema_release",
        "schema_sha256",
        "schema_head",
        "source_generation",
    }
)
READINESS_CHECK_NAMES = (
    "service_environment",
    "worker_environment",
    "filesystem_isolation",
    "artifact_binding",
    "spool",
    "kill_switch",
    "postgresql_connectivity",
    "database_identity",
    "alembic_head",
    "active_generation",
    "source_import",
    "import_binding",
    "open_baseline",
    "projection_epoch",
    "imported_corpus",
    "worker_unit",
)


class DarkLaunchReadinessError(ValueError):
    """The selected readiness input is unsafe, incomplete, or inconsistent."""


@dataclass(frozen=True)
class ArtifactBinding:
    receipt: Mapping[str, Any]
    records: tuple[SourceRecord, ...]
    source_sha256: str
    source_count: int
    manifest_count: int
    generation_id: uuid.UUID
    import_run_id: uuid.UUID
    binding_id: uuid.UUID
    baseline_id: uuid.UUID
    source_generation: str
    source_commit: str


@dataclass(frozen=True)
class PreflightInputs:
    service_environment: Path
    worker_environment: Path
    database_url: str
    expected_database_name: str
    manifest: Path
    legacy_ndjson: Path
    bootstrap_receipt: Path
    spool_path: Path
    kill_switch: Path
    unit_name: str
    repository_unit: Path
    state_root: Path = DEFAULT_STATE_ROOT
    config_root: Path = DEFAULT_CONFIG_ROOT
    evidence_root: Path = DEFAULT_EVIDENCE_ROOT
    report_path: Path | None = None
    expected_schema_head: str = DEFAULT_SCHEMA_HEAD
    systemctl_command: str = "systemctl"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = path.lstat()
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _sha256_path(path: Path) -> str:
    before = _file_identity(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if _file_identity(path) != before:
        raise DarkLaunchReadinessError(f"file changed while hashing: {path}")
    return digest.hexdigest()


def redact_reason(value: object, *, secrets: Sequence[str] = ()) -> str:
    rendered = _POSTGRES_USERINFO.sub(r"\g<prefix><redacted>@", str(value))
    for secret in sorted({item for item in secrets if len(item) >= 6}, key=len, reverse=True):
        rendered = rendered.replace(secret, "<redacted>")
    rendered = rendered.replace("\n", " ").replace("\r", " ")
    return rendered[:MAX_REASON_LENGTH]


def _check(
    *,
    passed: bool,
    reason: str,
    status: str | None = None,
    details: Mapping[str, Any] | None = None,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
    resolved_status = status or ("pass" if passed else "fail")
    value: dict[str, Any] = {
        "passed": bool(passed),
        "status": resolved_status,
        "reason": redact_reason(reason, secrets=secrets),
    }
    if details:
        value["details"] = dict(details)
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve(strict=False)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_canonical_json(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _owner_only_regular_file(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise DarkLaunchReadinessError(f"{label} must be an absolute path")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise DarkLaunchReadinessError(f"{label} is unavailable: {candidate}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DarkLaunchReadinessError(f"{label} must be a regular non-symlink file: {candidate}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DarkLaunchReadinessError(f"{label} must be owner-only: {candidate}")
    if metadata.st_uid != os.geteuid():
        raise DarkLaunchReadinessError(f"{label} must be owned by the executing user: {candidate}")
    if metadata.st_nlink != 1:
        raise DarkLaunchReadinessError(f"{label} must not have hard links: {candidate}")
    return candidate.resolve(strict=True)


def _owner_only_directory(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise DarkLaunchReadinessError(f"{label} must be an absolute path")
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise DarkLaunchReadinessError(f"{label} is unavailable: {candidate}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DarkLaunchReadinessError(f"{label} must be a non-symlink directory: {candidate}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise DarkLaunchReadinessError(f"{label} must be owner-only: {candidate}")
    if metadata.st_uid != os.geteuid():
        raise DarkLaunchReadinessError(f"{label} must be owned by the executing user: {candidate}")
    return candidate.resolve(strict=True)


def parse_environment_file(path: Path, *, label: str) -> dict[str, str]:
    source = _owner_only_regular_file(path, label=label)
    before = _file_identity(source)
    try:
        raw_lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DarkLaunchReadinessError(f"{label} is unreadable: {source}") from exc
    if _file_identity(source) != before:
        raise DarkLaunchReadinessError(f"{label} changed while it was read: {source}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(raw_lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise DarkLaunchReadinessError(
                f"invalid environment assignment at {source}:{line_number}"
            )
        if name in values:
            raise DarkLaunchReadinessError(
                f"duplicate environment variable {name} at {source}:{line_number}"
            )
        try:
            tokens = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise DarkLaunchReadinessError(
                f"invalid environment value at {source}:{line_number}: {exc}"
            ) from exc
        if len(tokens) > 1:
            raise DarkLaunchReadinessError(
                f"environment value must resolve to one token at {source}:{line_number}"
            )
        values[name] = tokens[0] if tokens else ""
        if len(values) > MAX_ENV_ASSIGNMENTS:
            raise DarkLaunchReadinessError(
                f"environment contains more than {MAX_ENV_ASSIGNMENTS} assignments"
            )
    return values


def execstart_variables(unit_text: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw_line in unit_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("ExecStart="):
            continue
        for name in _EXECSTART_VARIABLE.findall(line):
            if name not in values:
                values.append(name)
    if not values:
        raise DarkLaunchReadinessError("worker unit has no ExecStart environment variables")
    return tuple(values)


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        not lowered
        or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        or (lowered.startswith("<") and lowered.endswith(">"))
    )


def _positive_integer(values: Mapping[str, str], name: str) -> int:
    raw = values.get(name, "")
    try:
        value = int(raw)
    except ValueError as exc:
        raise DarkLaunchReadinessError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise DarkLaunchReadinessError(f"{name} must be a positive integer")
    return value


def validate_worker_environment(
    values: Mapping[str, str], *, unit_text: str
) -> dict[str, Any]:
    variables = execstart_variables(unit_text)
    missing = [name for name in variables if name not in values]
    if missing:
        raise DarkLaunchReadinessError(
            f"worker environment is missing ExecStart variables: {', '.join(sorted(missing))}"
        )
    forbidden = sorted(name for name in values if _forbidden_worker_name(name))
    if forbidden:
        raise DarkLaunchReadinessError(
            f"worker environment contains prohibited credential variables: {', '.join(forbidden)}"
        )
    unsupported = sorted(set(values) - set(variables))
    if unsupported:
        raise DarkLaunchReadinessError(
            "worker environment contains unsupported variables: "
            + ", ".join(unsupported)
        )
    placeholders = sorted(name for name in variables if _is_placeholder(values[name]))
    if placeholders:
        raise DarkLaunchReadinessError(
            f"worker environment contains missing or placeholder values: {', '.join(placeholders)}"
        )
    unsafe_values = sorted(
        name
        for name in variables
        if any(character.isspace() or character == "\x00" for character in values[name])
    )
    if unsafe_values:
        raise DarkLaunchReadinessError(
            "worker ExecStart values must not contain whitespace or NUL characters: "
            + ", ".join(unsafe_values)
        )
    for name in _NUMERIC_ENV_NAMES:
        if name not in values:
            raise DarkLaunchReadinessError(f"worker environment is missing {name}")
    numeric = {name: _positive_integer(values, name) for name in _NUMERIC_ENV_NAMES}
    reservation_ttl = numeric["DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS"]
    retention = numeric["DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS"]
    if reservation_ttl < 90:
        raise DarkLaunchReadinessError(
            "DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS must be at least 90"
        )
    if retention < reservation_ttl:
        raise DarkLaunchReadinessError(
            "DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS must be at least the reservation TTL"
        )
    try:
        baseline_id = str(uuid.UUID(values["DISH_DARK_LAUNCH_BASELINE_ID"]))
    except (KeyError, ValueError) as exc:
        raise DarkLaunchReadinessError(
            "DISH_DARK_LAUNCH_BASELINE_ID must be a UUID"
        ) from exc
    expected_database = values.get("DISH_PG_EXPECTED_DATABASE_NAME", "").strip()
    if not expected_database or _is_placeholder(expected_database):
        raise DarkLaunchReadinessError(
            "DISH_PG_EXPECTED_DATABASE_NAME must be an explicit database identity"
        )
    try:
        url = make_url(values["DISH_PG_DATABASE_URL"])
    except Exception as exc:
        raise DarkLaunchReadinessError("DISH_PG_DATABASE_URL is invalid") from exc
    if url.get_backend_name() != "postgresql":
        raise DarkLaunchReadinessError("DISH_PG_DATABASE_URL must select PostgreSQL")
    if url.database != expected_database:
        raise DarkLaunchReadinessError(
            "DISH_PG_DATABASE_URL database does not match DISH_PG_EXPECTED_DATABASE_NAME"
        )
    path_names = (
        "DISH_DARK_LAUNCH_SPOOL_PATH",
        "DISH_PG_CURSOR_SECRET_FILE",
        "DISH_DARK_LAUNCH_KILL_SWITCH",
    )
    for name in path_names:
        if not Path(values[name]).expanduser().is_absolute():
            raise DarkLaunchReadinessError(f"{name} must be an absolute path")
    return {
        "execstart_variables": list(variables),
        "database_name": expected_database,
        "baseline_id": baseline_id,
        "numeric_limits": numeric,
        "credential_variables_present": False,
    }


def _resolved_config_path(path: Path | None, *, label: str) -> Path:
    if path is None:
        raise DarkLaunchReadinessError(f"{label} is not configured")
    return path.expanduser().resolve(strict=False)


def require_production_service_environment(
    selected: Path,
    *,
    expected: Path | None = None,
) -> Path:
    selected_path = Path(os.path.abspath(selected.expanduser()))
    required_environment = (
        PRODUCTION_SERVICE_ENVIRONMENT if expected is None else expected
    )
    required_path = Path(os.path.abspath(required_environment.expanduser()))
    if selected_path != required_path:
        raise DarkLaunchReadinessError(
            "production readiness requires service environment "
            f"{required_path}; received {selected_path}"
        )
    return selected_path


def _load_service_config(service_values: Mapping[str, str]) -> ServiceConfig:
    try:
        return ServiceConfig.from_mapping(service_values)
    except Exception as exc:
        raise DarkLaunchReadinessError(
            "production service environment could not be parsed with ServiceConfig semantics"
        ) from exc


def validate_service_dark_launch_configuration(
    *,
    service_values: Mapping[str, str],
    worker_values: Mapping[str, str],
    inputs: PreflightInputs,
    service_config: ServiceConfig | None = None,
) -> dict[str, Any]:
    missing = [
        name
        for name in _SERVICE_REQUIRED_PATH_NAMES
        if not str(service_values.get(name, "")).strip()
    ]
    if missing:
        raise DarkLaunchReadinessError(
            "production service environment must explicitly define "
            + ", ".join(missing)
        )
    config = service_config or _load_service_config(service_values)
    if config.dark_launch_mode not in {"off", "capture"}:
        raise DarkLaunchReadinessError(
            "production service dark-launch mode must be off or capture during readiness"
        )

    service_paths = {
        "spool_path": _resolved_config_path(
            config.dark_launch_spool_path, label="service dark-launch spool"
        ),
        "emergency_directory": _resolved_config_path(
            config.dark_launch_emergency_dir,
            label="service dark-launch emergency directory",
        ),
        "kill_switch_path": _resolved_config_path(
            config.dark_launch_kill_switch_path,
            label="service dark-launch kill switch",
        ),
    }
    explicit_spool = inputs.spool_path.expanduser().resolve(strict=False)
    explicit_kill_switch = inputs.kill_switch.expanduser().resolve(strict=False)
    if service_paths["spool_path"] != explicit_spool:
        raise DarkLaunchReadinessError(
            "service effective spool path and preflight spool path disagree"
        )
    if service_paths["kill_switch_path"] != explicit_kill_switch:
        raise DarkLaunchReadinessError(
            "service effective kill-switch path and preflight kill-switch path disagree"
        )

    worker_spool = (
        Path(worker_values["DISH_DARK_LAUNCH_SPOOL_PATH"])
        .expanduser()
        .resolve(strict=False)
    )
    worker_kill_switch = (
        Path(worker_values["DISH_DARK_LAUNCH_KILL_SWITCH"])
        .expanduser()
        .resolve(strict=False)
    )
    if worker_spool != service_paths["spool_path"]:
        raise DarkLaunchReadinessError(
            "service and worker effective spool paths disagree"
        )
    if worker_kill_switch != service_paths["kill_switch_path"]:
        raise DarkLaunchReadinessError(
            "service and worker effective kill-switch paths disagree"
        )

    service_limits = {
        name: int(getattr(config, attribute))
        for name, attribute in _SERVICE_LIMIT_ATTRIBUTES.items()
    }
    worker_limits = {
        name: int(worker_values[name]) for name in _SERVICE_LIMIT_ATTRIBUTES
    }
    mismatched_limits = [
        name
        for name in _SERVICE_LIMIT_ATTRIBUTES
        if service_limits[name] != worker_limits[name]
    ]
    if mismatched_limits:
        raise DarkLaunchReadinessError(
            "service and worker effective dark-launch limits disagree for "
            + ", ".join(mismatched_limits)
        )
    return {
        "dark_launch_mode": config.dark_launch_mode,
        "spool_path": str(service_paths["spool_path"]),
        "emergency_directory": str(service_paths["emergency_directory"]),
        "kill_switch_path": str(service_paths["kill_switch_path"]),
        "numeric_limits": service_limits,
    }


def _reject_test_path(path: Path, *, label: str) -> None:
    if any(part.lower() == "test" for part in path.parts):
        raise DarkLaunchReadinessError(f"{label} must not use a TEST-root path: {path}")


def _under_root(path: Path, root: Path, *, label: str) -> Path:
    resolved_root = root.expanduser().resolve(strict=True)
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise DarkLaunchReadinessError(f"{label} must be an absolute path")
    candidate = Path(os.path.abspath(candidate))
    _reject_test_path(candidate, label=label)
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise DarkLaunchReadinessError(
            f"{label} must remain under approved root {resolved_root}: {candidate}"
        ) from exc

    current = resolved_root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise DarkLaunchReadinessError(
                f"{label} must not traverse a symlink: {current}"
            )
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise DarkLaunchReadinessError(
            f"{label} resolves outside approved root {resolved_root}: {candidate}"
        ) from exc
    return candidate


def _validate_report_destination(path: Path, *, evidence_root: Path) -> Path:
    resolved = _under_root(path, evidence_root, label="readiness report")
    _owner_only_directory(resolved.parent, label="readiness report parent")
    if resolved.exists():
        _owner_only_regular_file(resolved, label="readiness report")
    return resolved


def validate_production_paths(
    *,
    service_values: Mapping[str, str],
    worker_values: Mapping[str, str],
    inputs: PreflightInputs,
    service_config: ServiceConfig | None = None,
    expected_service_environment: Path | None = None,
) -> dict[str, str]:
    state_root = _owner_only_directory(inputs.state_root, label="production state root")
    config_root = _owner_only_directory(inputs.config_root, label="production config root")
    evidence_root = _owner_only_directory(inputs.evidence_root, label="production evidence root")

    require_production_service_environment(
        inputs.service_environment, expected=expected_service_environment
    )

    config = service_config or _load_service_config(service_values)
    effective = validate_service_dark_launch_configuration(
        service_values=service_values,
        worker_values=worker_values,
        inputs=inputs,
        service_config=config,
    )
    raw_sqlite = str(config.db_path)
    raw_emergency = effective["emergency_directory"]

    paths = {
        "production_sqlite": _owner_only_regular_file(
            _under_root(Path(raw_sqlite), state_root, label="production SQLite"),
            label="production SQLite",
        ),
        "spool": _owner_only_regular_file(
            _under_root(inputs.spool_path, state_root, label="dark-launch spool"),
            label="dark-launch spool",
        ),
        "emergency_directory": _owner_only_directory(
            _under_root(Path(raw_emergency), state_root, label="dark-launch emergency directory"),
            label="dark-launch emergency directory",
        ),
        "cursor_secret": _owner_only_regular_file(
            _under_root(
                Path(worker_values["DISH_PG_CURSOR_SECRET_FILE"]),
                config_root,
                label="cursor secret",
            ),
            label="cursor secret",
        ),
        "service_environment": _owner_only_regular_file(
            _under_root(inputs.service_environment, config_root, label="service environment"),
            label="service environment",
        ),
        "worker_environment": _owner_only_regular_file(
            _under_root(inputs.worker_environment, config_root, label="worker environment"),
            label="worker environment",
        ),
        "manifest": _owner_only_regular_file(
            _under_root(inputs.manifest, evidence_root, label="location manifest"),
            label="location manifest",
        ),
        "legacy_ndjson": _owner_only_regular_file(
            _under_root(inputs.legacy_ndjson, evidence_root, label="legacy NDJSON"),
            label="legacy NDJSON",
        ),
        "bootstrap_receipt": _owner_only_regular_file(
            _under_root(inputs.bootstrap_receipt, evidence_root, label="bootstrap receipt"),
            label="bootstrap receipt",
        ),
    }
    kill_switch = _under_root(inputs.kill_switch, state_root, label="kill switch")
    _owner_only_directory(kill_switch.parent, label="kill-switch parent")
    if kill_switch.exists():
        paths["kill_switch"] = _owner_only_regular_file(kill_switch, label="kill switch")
    else:
        paths["kill_switch"] = kill_switch
    if inputs.report_path is not None:
        paths["report"] = _validate_report_destination(
            inputs.report_path, evidence_root=evidence_root
        )
    try:
        require_distinct_paths({name: path for name, path in paths.items()})
    except PathIdentityError as exc:
        raise DarkLaunchReadinessError(str(exc)) from exc
    return {name: str(path) for name, path in paths.items()}


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DarkLaunchReadinessError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise DarkLaunchReadinessError(f"{label} root must be an object")
    return value


def _require_uuid(value: object, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise DarkLaunchReadinessError(f"{field} must be a UUID") from exc


def _receipt_source_commit(receipt: Mapping[str, Any]) -> str:
    release = str(receipt.get("dish_release") or "").strip()
    if not release.startswith("dish@") or len(release) <= len("dish@"):
        raise DarkLaunchReadinessError("bootstrap receipt dish_release must use dish@<identity>")
    return release.split("@", 1)[1]


def bind_artifacts(
    *,
    manifest_path: Path,
    ndjson_path: Path,
    receipt_path: Path,
    baseline_id: uuid.UUID,
    expected_schema_head: str = DEFAULT_SCHEMA_HEAD,
) -> ArtifactBinding:
    artifact_identities = {
        path: _file_identity(path)
        for path in (manifest_path, ndjson_path, receipt_path)
    }
    manifest = _load_json_object(manifest_path, label="location manifest")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise DarkLaunchReadinessError("location manifest must contain a non-empty tasks object")
    receipt = _load_json_object(receipt_path, label="bootstrap receipt")
    missing = sorted(_REQUIRED_RECEIPT_FIELDS - set(receipt))
    if missing:
        raise DarkLaunchReadinessError(
            f"bootstrap receipt is missing fields: {', '.join(missing)}"
        )
    receipt_schema_head = str(receipt["schema_head"] or "").strip()
    if receipt_schema_head != expected_schema_head:
        raise DarkLaunchReadinessError(
            "bootstrap receipt schema head does not match the expected Alembic head"
        )
    source_generation = str(receipt["source_generation"] or "").strip()
    if not source_generation or _is_placeholder(source_generation):
        raise DarkLaunchReadinessError(
            "bootstrap receipt source_generation must be an explicit identity"
        )
    for field in (
        "dish_release",
        "honest_release",
        "protocol_release",
        "schema_release",
    ):
        value = str(receipt[field] or "").strip()
        if not value or _is_placeholder(value):
            raise DarkLaunchReadinessError(
                f"bootstrap receipt {field} must be an explicit identity"
            )
    for field in ("protocol_sha256", "schema_sha256"):
        value = str(receipt[field] or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise DarkLaunchReadinessError(
                f"bootstrap receipt {field} must be a lowercase SHA-256"
            )
    source_sha256 = _sha256_path(ndjson_path)
    records = tuple(iter_source(ndjson_path))
    if not records:
        raise DarkLaunchReadinessError("legacy NDJSON contains no records")
    malformed = [record.identifier for record in records if record.error or record.spec is None]
    if malformed:
        raise DarkLaunchReadinessError(
            f"legacy NDJSON contains malformed records: {malformed[:MAX_ARTIFACT_ERRORS]}"
        )
    errors: list[str] = []
    observed_gids: set[str] = set()
    for record in records:
        assert record.spec is not None
        spec = record.spec
        gid = spec.asana_task_gid
        observed_gids.add(gid)
        location = tasks.get(gid)
        if not isinstance(location, Mapping):
            errors.append(f"task {gid}: missing manifest entry")
            continue
        expected = {
            "task_id": str(spec.task_id),
            "project_ids": [str(value) for value in spec.project_ids],
            "section_id": str(spec.section_id),
            "completed": spec.completed,
            "observed_at": spec.observed_at.isoformat(),
            "existence_state": spec.existence_state,
        }
        actual = {
            "task_id": str(location.get("task_id")),
            "project_ids": [str(value) for value in location.get("project_ids", [])]
            if isinstance(location.get("project_ids"), list)
            else None,
            "section_id": str(location.get("section_id")),
            "completed": location.get("completed"),
            "observed_at": str(location.get("observed_at")),
            "existence_state": str(location.get("existence_state", "ordinary")),
        }
        if actual != expected:
            errors.append(f"task {gid}: manifest and NDJSON location identity differ")
    extras = sorted(set(str(key) for key in tasks) - observed_gids)
    if extras:
        errors.append(f"manifest contains tasks absent from NDJSON: {extras[:MAX_ARTIFACT_ERRORS]}")
    if errors:
        raise DarkLaunchReadinessError(
            "; ".join(errors[:MAX_ARTIFACT_ERRORS])
        )
    expected_sha = str(receipt.get("source_bundle_sha256") or "")
    if source_sha256 != expected_sha:
        raise DarkLaunchReadinessError("NDJSON SHA-256 does not match bootstrap receipt")
    try:
        expected_count = int(receipt.get("source_record_count"))
    except (TypeError, ValueError) as exc:
        raise DarkLaunchReadinessError(
            "bootstrap receipt source_record_count must be an integer"
        ) from exc
    if expected_count != len(records) or len(tasks) != len(records):
        raise DarkLaunchReadinessError(
            "manifest, NDJSON, and bootstrap receipt record counts disagree"
        )
    changed = [
        str(path)
        for path, identity in artifact_identities.items()
        if _file_identity(path) != identity
    ]
    if changed:
        raise DarkLaunchReadinessError(
            f"readiness artifacts changed during inspection: {', '.join(changed)}"
        )
    return ArtifactBinding(
        receipt=receipt,
        records=records,
        source_sha256=source_sha256,
        source_count=len(records),
        manifest_count=len(tasks),
        generation_id=_require_uuid(receipt["generation_id"], field="receipt.generation_id"),
        import_run_id=_require_uuid(receipt["import_run_id"], field="receipt.import_run_id"),
        binding_id=_require_uuid(receipt["binding_id"], field="receipt.binding_id"),
        baseline_id=baseline_id,
        source_generation=source_generation,
        source_commit=_receipt_source_commit(receipt),
    )


def _authority_checks(session: Session, *, binding: ArtifactBinding) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    active_generations = tuple(
        session.scalars(
            select(models.AuthorityGeneration).where(
                models.AuthorityGeneration.status == "active"
            ).limit(2)
        )
    )
    generation = active_generations[0] if len(active_generations) == 1 else None
    generation_ok = (
        generation is not None
        and generation.generation_id == binding.generation_id
        and generation.schema_head == binding.receipt["schema_head"]
        and generation.dish_release == binding.receipt["dish_release"]
    )
    checks["active_generation"] = _check(
        passed=generation_ok,
        reason=(
            "active generation exactly matches the bootstrap receipt"
            if generation_ok
            else "active generation count or identity does not match the bootstrap receipt"
        ),
        details={
            "active_generation_count": len(active_generations),
            "generation_id": None if generation is None else str(generation.generation_id),
        },
    )

    import_run = session.get(models.ImportRun, binding.import_run_id)
    import_ok = bool(
        import_run is not None
        and import_run.status == "complete"
        and import_run.source_commit == binding.source_commit
        and import_run.source_release == binding.receipt["dish_release"]
        and import_run.legacy_generation_id == binding.source_generation
        and import_run.source_bundle_sha256 == binding.source_sha256
        and import_run.provenance.get("source_record_count") == binding.source_count
    )
    checks["source_import"] = _check(
        passed=import_ok,
        reason=(
            "complete import run matches source generation, SHA-256, and count"
            if import_ok
            else "import run does not match the receipt-bound source identity"
        ),
        details={"import_run_id": str(binding.import_run_id)},
    )

    contract = session.get(models.HonestContractBinding, binding.binding_id)
    receipt = binding.receipt
    contract_ok = bool(
        contract is not None
        and contract.binding_kind == "release"
        and contract.source_identity == receipt["honest_release"]
        and contract.dish_release == receipt["dish_release"]
        and contract.honest_release == receipt["honest_release"]
        and contract.protocol_release == receipt["protocol_release"]
        and contract.protocol_sha256 == receipt["protocol_sha256"]
        and contract.schema_release == receipt["schema_release"]
        and contract.schema_sha256 == receipt["schema_sha256"]
    )
    active_registry = session.get(models.ActiveSectionRegistry, binding.generation_id)
    registry = (
        None
        if active_registry is None
        else session.get(models.SectionRegistryVersion, active_registry.registry_version_id)
    )
    registry_ok = bool(
        registry is not None
        and registry.import_run_id == binding.import_run_id
        and registry.contract_binding_id == binding.binding_id
        and registry.generation_id == binding.generation_id
    )
    activation = (
        None
        if active_registry is None
        else session.get(
            models.SectionRegistryActivation,
            active_registry.registry_activation_id,
        )
    )
    activation_ok = bool(
        activation is not None
        and activation.generation_id == binding.generation_id
        and activation.registry_version_id == active_registry.registry_version_id
        and activation.activation_route == "import"
        and activation.import_run_id == binding.import_run_id
        and activation.command_execution_id is None
        and activation.registry_revision == active_registry.registry_revision
    )
    checks["import_binding"] = _check(
        passed=contract_ok and registry_ok and activation_ok,
        reason=(
            "contract binding and active section registry match the receipt"
            if contract_ok and registry_ok and activation_ok
            else "contract binding or active section registry identity is inconsistent"
        ),
        details={"binding_id": str(binding.binding_id)},
    )

    baseline = session.get(tx.ShadowBaseline, binding.baseline_id)
    baseline_ok = bool(
        baseline is not None
        and baseline.status == "open"
        and baseline.generation_id == binding.generation_id
        and baseline.source_generation_identity == binding.source_generation
        and baseline.source_commit == binding.source_commit
    )
    checks["open_baseline"] = _check(
        passed=baseline_ok,
        reason=(
            "open shadow baseline matches generation and source identity"
            if baseline_ok
            else "shadow baseline is absent, closed, stale, or source-mismatched"
        ),
        details={"shadow_baseline_id": str(binding.baseline_id)},
    )

    epochs = tuple(
        session.scalars(
            select(tx.ProjectionEpoch).where(
                tx.ProjectionEpoch.generation_id == binding.generation_id,
                tx.ProjectionEpoch.status == "active",
            ).limit(2)
        )
    )
    epoch = epochs[0] if len(epochs) == 1 else None
    epoch_ok = bool(epoch is not None and epoch.external_effects_enabled is False)
    checks["projection_epoch"] = _check(
        passed=epoch_ok,
        reason=(
            "one active projection epoch exists with external effects disabled"
            if epoch_ok
            else "active projection epoch is absent, duplicated, or external effects are enabled"
        ),
        details={
            "active_epoch_count": len(epochs),
            "projection_epoch_id": None if epoch is None else str(epoch.projection_epoch_id),
            "external_effects_enabled": None if epoch is None else epoch.external_effects_enabled,
        },
    )

    verification_errors = verify_imported_records(
        session,
        records=binding.records,
        generation_id=binding.generation_id,
        import_run_id=binding.import_run_id,
        contract_binding_id=binding.binding_id,
    )
    imported_count = int(session.scalar(select(func.count()).select_from(models.DishTask)) or 0)
    imported_ok = not verification_errors and imported_count == binding.source_count
    checks["imported_corpus"] = _check(
        passed=imported_ok,
        reason=(
            "every NDJSON record matches the imported PostgreSQL authority head"
            if imported_ok
            else "imported PostgreSQL corpus differs from the receipt-bound NDJSON"
        ),
        details={
            "expected_records": binding.source_count,
            "database_records": imported_count,
            "verification_error_count": len(verification_errors),
            "verification_errors": verification_errors[:MAX_ARTIFACT_ERRORS],
        },
    )
    return checks


def inspect_postgresql_read_only(
    *,
    engine: Engine,
    expected_database_name: str,
    expected_schema_head: str,
    binding: ArtifactBinding,
    authority_reader: Callable[[Session], dict[str, dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    if engine.dialect.name != "postgresql":
        raise DarkLaunchReadinessError("readiness preflight requires native PostgreSQL")
    url = make_url(str(engine.url))
    if url.database != expected_database_name:
        raise DarkLaunchReadinessError(
            "selected PostgreSQL URL does not match the expected database identity"
        )
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            database_name = str(connection.scalar(text("SELECT current_database()")))
            heads = tuple(
                str(value)
                for value in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
            )
            checks: dict[str, dict[str, Any]] = {
                "postgresql_connectivity": _check(
                    passed=True,
                    reason="native PostgreSQL connection succeeded in a read-only transaction",
                    details={"database_name": database_name},
                ),
                "database_identity": _check(
                    passed=database_name == expected_database_name,
                    reason=(
                        "database identity matches exactly"
                        if database_name == expected_database_name
                        else "database identity does not match the expected production database"
                    ),
                    details={
                        "expected_database_name": expected_database_name,
                        "actual_database_name": database_name,
                    },
                ),
                "alembic_head": _check(
                    passed=heads == (expected_schema_head,),
                    reason=(
                        "Alembic head matches exactly"
                        if heads == (expected_schema_head,)
                        else "Alembic head is absent, divergent, or duplicated"
                    ),
                    details={
                        "expected_schema_head": expected_schema_head,
                        "actual_schema_heads": list(heads),
                    },
                ),
            }
            session = Session(bind=connection, autoflush=False, expire_on_commit=False, future=True)
            try:
                reader = authority_reader or (
                    lambda current: _authority_checks(current, binding=binding)
                )
                checks.update(reader(session))
            finally:
                session.close()
            return checks
        finally:
            transaction.rollback()


def parse_systemctl_show(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in output.splitlines()[:64]:
        key, separator, value = raw_line.partition("=")
        if not separator or not key:
            continue
        values[key] = value.strip()
    return values


def _systemd_paths(value: str, *, property_name: str) -> tuple[str, ...]:
    try:
        tokens = shlex.split(value, comments=False, posix=True)
    except ValueError as exc:
        raise DarkLaunchReadinessError(
            f"systemctl show returned invalid {property_name} quoting"
        ) from exc
    paths: list[str] = []
    for token in tokens:
        candidate = token.strip("{};,")
        if candidate.startswith("path="):
            candidate = candidate.split("=", 1)[1]
        candidate = candidate.lstrip("-")
        if candidate.startswith("/"):
            paths.append(candidate)
            if len(paths) > MAX_SYSTEMD_PATHS:
                raise DarkLaunchReadinessError(
                    f"systemctl show returned too many {property_name} paths"
                )
    return tuple(paths)


def observe_worker_unit(
    *,
    unit_name: str,
    systemctl_command: str = "systemctl",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Read bounded systemd unit state without invoking a mutating verb."""
    command = [
        systemctl_command,
        "show",
        unit_name,
        "--no-pager",
        "--property=LoadState,ActiveState,SubState,UnitFileState,FragmentPath,Result,"
        "EnvironmentFiles,Environment,PassEnvironment,DropInPaths",
    ]
    completed = runner(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if completed.returncode != 0:
        raise DarkLaunchReadinessError(
            f"systemctl show failed for {unit_name}: {redact_reason(completed.stderr)}"
        )
    if (
        len(completed.stdout.encode("utf-8", errors="replace"))
        > MAX_SYSTEMCTL_OUTPUT_BYTES
        or len(completed.stderr.encode("utf-8", errors="replace"))
        > MAX_SYSTEMCTL_OUTPUT_BYTES
    ):
        raise DarkLaunchReadinessError("systemctl show output exceeded the bounded limit")
    values = parse_systemctl_show(completed.stdout)
    required = {
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
        "FragmentPath",
        "Result",
        "EnvironmentFiles",
        "Environment",
        "PassEnvironment",
        "DropInPaths",
    }
    missing = sorted(required - set(values))
    if missing:
        raise DarkLaunchReadinessError(
            f"systemctl show omitted required properties: {', '.join(missing)}"
        )
    return {
        "unit_name": unit_name,
        "load_state": values["LoadState"],
        "active_state": values["ActiveState"],
        "sub_state": values["SubState"],
        "unit_file_state": values["UnitFileState"],
        "result": values["Result"],
        "fragment_path": values["FragmentPath"],
        "environment_files": list(
            _systemd_paths(values["EnvironmentFiles"], property_name="EnvironmentFiles")
        ),
        "inline_environment_present": bool(values["Environment"].strip()),
        "pass_environment_present": bool(values["PassEnvironment"].strip()),
        "drop_in_paths": list(
            _systemd_paths(values["DropInPaths"], property_name="DropInPaths")
        ),
        "command": command,
    }


def inspect_worker_unit(
    *,
    unit_name: str,
    repository_unit: Path,
    expected_environment_file: Path,
    systemctl_command: str = "systemctl",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    observed = observe_worker_unit(
        unit_name=unit_name,
        systemctl_command=systemctl_command,
        runner=runner,
    )
    repository = repository_unit.expanduser().resolve(strict=True)
    loaded = observed["load_state"] == "loaded"
    inactive = observed["active_state"] == "inactive" and observed["sub_state"] in {
        "dead",
        "exited",
    }
    failed = observed["active_state"] == "failed" or observed["result"] not in {
        "",
        "success",
    }
    disabled = observed["unit_file_state"] == "disabled"
    configured_environment_files = tuple(
        Path(value).expanduser().resolve(strict=False)
        for value in observed["environment_files"]
    )
    expected_environment = expected_environment_file.expanduser().resolve(strict=True)
    environment_file_matches = configured_environment_files == (expected_environment,)
    environment_isolated = (
        environment_file_matches
        and not observed["inline_environment_present"]
        and not observed["pass_environment_present"]
        and not observed["drop_in_paths"]
    )
    fragment_path = str(observed["fragment_path"]).strip()
    repository_sha256 = _sha256_path(repository)
    installed: Path | None = None
    installed_sha256: str | None = None
    installed_safe = False
    installed_error: str | None = None
    if loaded and fragment_path:
        try:
            installed = _owner_only_or_root_regular_file(
                Path(fragment_path).expanduser(), label="installed worker unit"
            )
            installed_sha256 = _sha256_path(installed)
            installed_safe = True
        except (OSError, DarkLaunchReadinessError) as exc:
            installed_error = redact_reason(exc)
    digest_matches = installed_sha256 == repository_sha256
    passed = (
        loaded
        and installed_safe
        and inactive
        and not failed
        and disabled
        and digest_matches
        and environment_isolated
    )
    details = {
        **{key: value for key, value in observed.items() if key != "command"},
        "fragment_path": None if installed is None else str(installed),
        "repository_sha256": repository_sha256,
        "installed_sha256": installed_sha256,
        "digest_matches": digest_matches,
        "expected_environment_file": str(expected_environment),
        "environment_files": [str(value) for value in configured_environment_files],
        "environment_file_matches": environment_file_matches,
        "inline_environment_present": observed["inline_environment_present"],
        "pass_environment_present": observed["pass_environment_present"],
        "drop_in_paths": observed["drop_in_paths"],
    }
    if installed_error is not None:
        details["installed_unit_error"] = installed_error
    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "reason": (
            "installed worker unit matches the repository unit and is disabled, "
            "stopped, healthy, and environment-isolated"
            if passed
            else (
                "installed worker unit is missing, unsafe, divergent, enabled, active, failed, "
                "or modified by an unexpected environment source"
            )
        ),
        "details": details,
        "command": observed["command"],
    }


def _owner_only_or_root_regular_file(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise DarkLaunchReadinessError(f"{label} must be an absolute path")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DarkLaunchReadinessError(f"{label} must be a regular non-symlink file")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise DarkLaunchReadinessError(f"{label} has an unexpected owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise DarkLaunchReadinessError(f"{label} must not be group/world writable")
    return path.resolve(strict=True)


def _secret_values(values: Mapping[str, str]) -> tuple[str, ...]:
    secrets: list[str] = []
    for name, value in values.items():
        upper = name.upper()
        if (
            upper in {"ASANA_PAT", "ASANA_ACCESS_TOKEN"}
            or any(marker in upper for marker in ("TOKEN", "PASSWORD", "SECRET"))
        ):
            if value:
                secrets.append(value)
        if upper.endswith("DATABASE_URL") and value:
            try:
                password = make_url(value).password
            except Exception:
                password = None
            if password:
                secrets.append(password)
    return tuple(secrets)


def run_preflight(
    inputs: PreflightInputs,
    *,
    engine_factory: Callable[[DatabaseSettings], Engine] = create_database_engine,
    systemctl_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now: Callable[[], datetime] = _utcnow,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    service_values: dict[str, str] = {}
    worker_values: dict[str, str] = {}
    secrets: tuple[str, ...] = ()
    binding: ArtifactBinding | None = None
    report_destination: Path | None = None
    service_parse_error: Exception | None = None

    try:
        service_values = parse_environment_file(
            inputs.service_environment, label="production service environment"
        )
        secrets = _secret_values(service_values)
    except Exception as exc:
        service_parse_error = exc
        checks["service_environment"] = _check(
            passed=False, reason=str(exc), secrets=secrets
        )

    try:
        worker_values = parse_environment_file(
            inputs.worker_environment, label="dark-launch worker environment"
        )
        secrets = _secret_values(service_values) + _secret_values(worker_values)
        unit_text = inputs.repository_unit.read_text(encoding="utf-8")
        contract = validate_worker_environment(worker_values, unit_text=unit_text)
        if worker_values["DISH_PG_DATABASE_URL"] != inputs.database_url:
            raise DarkLaunchReadinessError(
                "explicit PostgreSQL URL and worker environment PostgreSQL URL disagree"
            )
        if contract["database_name"] != inputs.expected_database_name:
            raise DarkLaunchReadinessError(
                "explicit expected database name and worker environment identity disagree"
            )
        checks["worker_environment"] = _check(
            passed=True,
            reason=(
                "worker environment defines every ExecStart variable and passes "
                "the safety contract"
            ),
            details=contract,
            secrets=secrets,
        )
    except Exception as exc:
        checks["worker_environment"] = _check(
            passed=False, reason=str(exc), secrets=secrets
        )

    service_config: ServiceConfig | None = None
    try:
        if service_parse_error is not None:
            raise service_parse_error
        require_production_service_environment(inputs.service_environment)
        service_config = _load_service_config(service_values)
        service_contract = validate_service_dark_launch_configuration(
            service_values=service_values,
            worker_values=worker_values,
            inputs=inputs,
            service_config=service_config,
        )
        checks["service_environment"] = _check(
            passed=True,
            reason=(
                "production service environment has an effective dark-launch "
                "configuration matching the preflight and worker"
            ),
            details=service_contract,
            secrets=secrets,
        )
    except Exception as exc:
        checks["service_environment"] = _check(
            passed=False, reason=str(exc), secrets=secrets
        )

    try:
        path_details = validate_production_paths(
            service_values=service_values,
            worker_values=worker_values,
            inputs=inputs,
            service_config=service_config,
        )
        if inputs.report_path is not None:
            report_destination = Path(path_details["report"])
        checks["filesystem_isolation"] = _check(
            passed=True,
            reason=(
                "production paths are owner-safe, non-TEST, approved-root confined, "
                "and non-aliased"
            ),
            details=path_details,
            secrets=secrets,
        )
    except Exception as exc:
        checks["filesystem_isolation"] = _check(
            passed=False, reason=str(exc), secrets=secrets
        )

    try:
        baseline_id = uuid.UUID(worker_values["DISH_DARK_LAUNCH_BASELINE_ID"])
        binding = bind_artifacts(
            manifest_path=inputs.manifest,
            ndjson_path=inputs.legacy_ndjson,
            receipt_path=inputs.bootstrap_receipt,
            baseline_id=baseline_id,
            expected_schema_head=inputs.expected_schema_head,
        )
        checks["artifact_binding"] = _check(
            passed=True,
            reason=(
                "manifest, NDJSON, bootstrap receipt, generation, import, and "
                "binding identities agree"
            ),
            details={
                "source_sha256": binding.source_sha256,
                "source_record_count": binding.source_count,
                "generation_id": str(binding.generation_id),
                "import_run_id": str(binding.import_run_id),
                "binding_id": str(binding.binding_id),
                "shadow_baseline_id": str(binding.baseline_id),
            },
            secrets=secrets,
        )
    except Exception as exc:
        checks["artifact_binding"] = _check(
            passed=False, reason=str(exc), secrets=secrets
        )

    try:
        limits = {name: int(worker_values[name]) for name in _NUMERIC_ENV_NAMES}
        spool_identity = _file_identity(inputs.spool_path)
        spool = ShadowSpool.open_existing_read_only(
            inputs.spool_path,
            busy_timeout_ms=limits["DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS"],
            max_bytes=limits["DISH_DARK_LAUNCH_MAX_SPOOL_BYTES"],
            max_records=limits["DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS"],
            min_free_bytes=limits["DISH_DARK_LAUNCH_MIN_FREE_BYTES"],
        )
        spool_status = dict(spool.status())
        if _file_identity(inputs.spool_path) != spool_identity:
            raise DarkLaunchReadinessError("spool changed during read-only inspection")
        for suffix in ("-journal", "-wal"):
            if Path(f"{inputs.spool_path}{suffix}").exists():
                raise DarkLaunchReadinessError(
                    f"spool acquired a SQLite {suffix[1:]} sidecar during inspection"
                )
        capacity = dict(spool_status["capacity"])
        accepting_new_records = bool(capacity.get("accepting_new_records"))
        checks["spool"] = _check(
            passed=accepting_new_records,
            reason=(
                "existing spool opened read-only and is within the configured capacity limits"
                if accepting_new_records
                else "existing spool is at or beyond a configured capacity or free-space limit"
            ),
            details={
                "counts": spool_status["counts"],
                "capacity": capacity,
                "oldest_pending_sequence": spool_status["oldest_pending_sequence"],
                "oldest_pending_at": spool_status["oldest_pending_at"],
            },
            secrets=secrets,
        )
    except Exception as exc:
        checks["spool"] = _check(passed=False, reason=str(exc), secrets=secrets)

    kill_state = inspect_kill_switch(inputs.kill_switch)
    kill_ready = kill_state["state"] == "clear"
    checks["kill_switch"] = _check(
        passed=kill_ready,
        reason=(
            "kill switch is clear"
            if kill_ready
            else "kill switch is engaged or invalid; explicit Marco-authorized action is required"
        ),
        details=kill_state,
        secrets=secrets,
    )

    if binding is not None:
        engine: Engine | None = None
        try:
            engine = engine_factory(DatabaseSettings(url=inputs.database_url))
            checks.update(
                inspect_postgresql_read_only(
                    engine=engine,
                    expected_database_name=inputs.expected_database_name,
                    expected_schema_head=inputs.expected_schema_head,
                    binding=binding,
                )
            )
        except Exception as exc:
            checks["postgresql_connectivity"] = _check(
                passed=False,
                status="unavailable",
                reason=str(exc),
                secrets=secrets,
            )
        finally:
            if engine is not None:
                engine.dispose()
    else:
        checks["postgresql_connectivity"] = _check(
            passed=False,
            status="unavailable",
            reason="artifact binding failed before PostgreSQL identity checks could run",
            secrets=secrets,
        )

    try:
        unit = inspect_worker_unit(
            unit_name=inputs.unit_name,
            repository_unit=inputs.repository_unit,
            expected_environment_file=inputs.worker_environment,
            systemctl_command=inputs.systemctl_command,
            runner=systemctl_runner,
        )
        unit.pop("command", None)
        checks["worker_unit"] = unit
    except Exception as exc:
        checks["worker_unit"] = _check(
            passed=False,
            status="unavailable",
            reason=str(exc),
            secrets=secrets,
        )

    for check_name in READINESS_CHECK_NAMES:
        checks.setdefault(
            check_name,
            _check(
                passed=False,
                status="unavailable",
                reason=(
                    f"{check_name.replace('_', ' ')} was not reached because an "
                    "earlier prerequisite failed"
                ),
                secrets=secrets,
            ),
        )

    unavailable = any(item["status"] == "unavailable" for item in checks.values())
    failed = any(
        not item["passed"] and item["status"] != "unavailable"
        for item in checks.values()
    )
    status = "not_ready" if failed else ("blocked" if unavailable else "ready")
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "observed_at": now().astimezone(timezone.utc).isoformat(),
        "status": status,
        "ready": status == "ready",
        "read_only": True,
        "production_mutated": False,
        "unit_identity": inputs.unit_name,
        "expected_database_name": inputs.expected_database_name,
        "expected_schema_head": inputs.expected_schema_head,
        "checks": checks,
    }
    report["report_sha256"] = _sha256_bytes(_canonical_json(report))
    if inputs.report_path is not None:
        try:
            if report_destination is None:
                raise DarkLaunchReadinessError(
                    "readiness report path was not validated; no report file was written"
                )
            _atomic_json(report_destination, report)
        except Exception as exc:
            report["status"] = "not_ready"
            report["ready"] = False
            report["checks"]["report_output"] = _check(
                passed=False, reason=str(exc), secrets=secrets
            )
            report["report_sha256"] = _sha256_bytes(
                _canonical_json(
                    {
                        key: value
                        for key, value in report.items()
                        if key != "report_sha256"
                    }
                )
            )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dish-pg-dark-launch-readiness",
        description=(
            "Run a machine-readable, strictly read-only PostgreSQL dark-launch "
            "readiness preflight."
        ),
    )
    parser.add_argument("--service-environment", required=True, type=Path)
    parser.add_argument("--worker-environment", required=True, type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--legacy-ndjson", required=True, type=Path)
    parser.add_argument("--bootstrap-receipt", required=True, type=Path)
    parser.add_argument("--spool-path", required=True, type=Path)
    parser.add_argument("--kill-switch", required=True, type=Path)
    parser.add_argument("--unit-name", default=DEFAULT_UNIT_NAME)
    parser.add_argument("--repository-unit", type=Path, default=DEFAULT_REPOSITORY_UNIT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--config-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--expected-schema-head", default=DEFAULT_SCHEMA_HEAD)
    parser.add_argument("--systemctl-command", default="systemctl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    inputs = PreflightInputs(
        service_environment=args.service_environment,
        worker_environment=args.worker_environment,
        database_url=args.database_url,
        expected_database_name=args.expected_database_name,
        manifest=args.manifest,
        legacy_ndjson=args.legacy_ndjson,
        bootstrap_receipt=args.bootstrap_receipt,
        spool_path=args.spool_path,
        kill_switch=args.kill_switch,
        unit_name=args.unit_name,
        repository_unit=args.repository_unit,
        state_root=args.state_root,
        config_root=args.config_root,
        evidence_root=args.evidence_root,
        report_path=args.report_path,
        expected_schema_head=args.expected_schema_head,
        systemctl_command=args.systemctl_command,
    )
    report = run_preflight(inputs)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
