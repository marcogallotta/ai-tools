"""Capture exact Asana location manifests for the legacy PostgreSQL export.

TEST capture preserves the existing fixed TEST authority path. Production capture
is an explicit, fail-closed, read-only path bound to the fixed production service
environment, production Cooking project, and production SQLite state root.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from dish_tool.identifiers import (
    ASANA_IDENTITY_NAMESPACE,
    ASANA_IDENTITY_SCHEME as IDENTITY_SCHEME,
    stable_dish_uuid_for_asana_identity,
)

TEST_ENV_FILE = Path("/home/marco/.config/dish-service/test.env")
PRODUCTION_ENV_FILE = Path("/home/marco/.config/dish-service/prod.env")
TEST_STATE_ROOT = Path("/home/marco/.local/state/dish/test")
PRODUCTION_STATE_ROOT = Path("/home/marco/.local/state/dish/prod")
TEST_COOKING_PROJECT_GID = "1216693403164366"
PRODUCTION_COOKING_PROJECT_GID = "1217084805070730"
PRODUCTION_ASANA_ENV_FILE = Path("/home/marco/.config/asana-cli/.env")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENTITY_KINDS = frozenset({"task", "project", "section"})
_CAPTURE_ENVIRONMENTS = frozenset({"test", "production"})
_ASANA_OPT_FIELDS = ",".join(
    (
        "gid",
        "completed",
        "memberships.project.gid",
        "memberships.section.gid",
    )
)


class LocationManifestError(ValueError):
    """A fail-closed manifest capture or environment-validation error."""


def _canonical_gid(value: object, *, field: str) -> str:
    gid = str(value or "").strip()
    if not gid.isdigit() or gid.startswith("0"):
        raise LocationManifestError(f"{field} must be a canonical positive decimal Asana GID")
    return gid


def target_uuid(entity_kind: str, asana_gid: object) -> uuid.UUID:
    """Return the stable target UUID for one typed Asana identity."""
    if entity_kind not in _ENTITY_KINDS:
        raise LocationManifestError(f"unsupported Asana identity kind: {entity_kind}")
    gid = _canonical_gid(asana_gid, field=f"{entity_kind}_gid")
    return stable_dish_uuid_for_asana_identity(entity_kind, gid)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _reject_symlink_components(path: Path, *, label: str) -> Path:
    absolute = _absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise LocationManifestError(f"{label} is unavailable: {absolute}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LocationManifestError(f"{label} must not use symlinks: {absolute}")
    return absolute


def _resolve_existing(path: Path, *, label: str, reject_symlinks: bool = False) -> Path:
    candidate = _reject_symlink_components(path, label=label) if reject_symlinks else path.expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LocationManifestError(f"{label} is unavailable: {candidate}: {exc}") from exc
    if reject_symlinks:
        try:
            metadata = resolved.stat()
        except OSError as exc:
            raise LocationManifestError(f"{label} is unavailable: {resolved}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise LocationManifestError(f"{label} must be a regular non-symlink file: {resolved}")
    return resolved


def _task_gids(database: Path, *, require_nonzero: bool = False) -> tuple[str, ...]:
    resolved = _resolve_existing(database, label="SQLite authority database")
    uri = f"file:{resolved}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT task_gid FROM task_content_state ORDER BY task_gid"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LocationManifestError(f"cannot read SQLite task corpus: {exc}") from exc
    task_gids = tuple(
        _canonical_gid(row[0], field="task_content_state.task_gid") for row in rows
    )
    if require_nonzero and not task_gids:
        raise LocationManifestError("production SQLite task corpus must be non-zero")
    return task_gids


def _required_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocationManifestError(f"Asana returned malformed {field}")
    return value


def _project_section(
    task: Mapping[str, Any], project_gid: str, *, environment: str
) -> str | None:
    """Return the task's current section within `project_gid`, or None if the task has
    since left that project's membership (normal lifecycle -- e.g. moved to Cooking
    History once eaten). A departed task is still governed by dish; the caller falls
    back to its last known section rather than treating this as an error."""
    memberships = task.get("memberships")
    if not isinstance(memberships, list):
        raise LocationManifestError("Asana returned malformed task memberships")
    sections: set[str] = set()
    matching_memberships = 0
    for raw_membership in memberships:
        membership = _required_mapping(raw_membership, field="task membership")
        project = _required_mapping(membership.get("project"), field="membership project")
        raw_project_gid = str(project.get("gid") or "").strip()
        if environment == "test":
            if raw_project_gid != project_gid:
                continue
        else:
            membership_project_gid = _canonical_gid(
                raw_project_gid, field="membership.project.gid"
            )
            if membership_project_gid != project_gid:
                continue
        matching_memberships += 1
        section = _required_mapping(membership.get("section"), field="membership section")
        sections.add(_canonical_gid(section.get("gid"), field="membership.section.gid"))
    label = "TEST" if environment == "test" else "production"
    if matching_memberships == 0:
        return None
    if matching_memberships != 1 or len(sections) != 1:
        raise LocationManifestError(
            f"task has ambiguous placement in {label} project {project_gid}: "
            f"sections={sorted(sections)}"
        )
    return next(iter(sections))


def _last_known_section_gid(database: Path, task_gid: str) -> str | None:
    """Most recent confirmed section movement recorded locally for `task_gid`, used
    as the section for a task that has since left the live project's membership."""
    resolved = _resolve_existing(database, label="SQLite authority database")
    uri = f"file:{resolved}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            row = connection.execute(
                """
                SELECT movement_attempts.confirmed_section_gid
                  FROM movement_attempts
                  JOIN operations ON operations.operation_id = movement_attempts.operation_id
                 WHERE operations.task_gid = ?
                   AND movement_attempts.outcome = 'confirmed'
                   AND movement_attempts.confirmed_section_gid IS NOT NULL
                 ORDER BY movement_attempts.finished_at DESC
                 LIMIT 1
                """,
                (task_gid,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise LocationManifestError(f"cannot read last known section: {exc}") from exc
    if row is None:
        return None
    return _canonical_gid(row[0], field="movement_attempts.confirmed_section_gid")


def build_location_manifest(
    *,
    database: Path,
    project_gid: str,
    read_task: Callable[[str], Mapping[str, Any]],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    environment: str = "test",
    require_nonzero: bool = False,
) -> dict[str, object]:
    """Read every SQLite task from Asana and build an exact corpus manifest."""
    if environment not in _CAPTURE_ENVIRONMENTS:
        raise LocationManifestError(f"unsupported capture environment: {environment}")
    canonical_project_gid = _canonical_gid(project_gid, field="DISH_COOKING_PROJECT_GID")
    tasks: dict[str, dict[str, object]] = {}
    for task_gid in _task_gids(database, require_nonzero=require_nonzero):
        task = _required_mapping(read_task(task_gid), field=f"task {task_gid}")
        response_gid = _canonical_gid(task.get("gid"), field="task.gid")
        if response_gid != task_gid:
            raise LocationManifestError(
                f"Asana task identity mismatch requested={task_gid} returned={response_gid}"
            )
        completed = task.get("completed")
        if not isinstance(completed, bool):
            raise LocationManifestError(f"task {task_gid} completed must be a boolean")
        section_gid = _project_section(
            task, canonical_project_gid, environment=environment
        )
        if section_gid is None:
            section_gid = _last_known_section_gid(database, task_gid)
            if section_gid is None:
                raise LocationManifestError(
                    f"task {task_gid} has left the project and has no last known section"
                )
        observed_at = now()
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise LocationManifestError("observation clock must return a timezone-aware datetime")
        tasks[task_gid] = {
            "task_id": str(target_uuid("task", task_gid)),
            "project_ids": [str(target_uuid("project", canonical_project_gid))],
            "section_id": str(target_uuid("section", section_gid)),
            "section_gid": section_gid,
            "completed": completed,
            "observed_at": observed_at.isoformat(),
            "existence_state": "ordinary",
        }
    return {"tasks": tasks}


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return left.resolve(strict=False) == right.resolve(strict=False)


def _safe_output_path(path: Path, *, protected_paths: tuple[Path, ...]) -> Path:
    destination = _reject_symlink_components(path, label="manifest output")
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise LocationManifestError(f"manifest output is unavailable: {destination}: {exc}") from exc
    if metadata is not None and not stat.S_ISREG(metadata.st_mode):
        raise LocationManifestError(
            f"manifest output must be a regular non-symlink file: {destination}"
        )
    for protected in protected_paths:
        if _same_file(destination, protected):
            raise LocationManifestError(
                f"manifest output must not alias protected input: {destination}"
            )
    return destination


def _output_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LocationManifestError(f"manifest output is unavailable: {path}: {exc}") from exc
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _atomic_json(
    path: Path,
    value: Mapping[str, object],
    *,
    protected_paths: tuple[Path, ...] = (),
) -> None:
    destination = (
        _safe_output_path(path, protected_paths=protected_paths)
        if protected_paths
        else path.expanduser().resolve()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if protected_paths:
        _reject_symlink_components(destination.parent, label="manifest output parent")
    initial_identity = _output_identity(destination) if protected_paths else None
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if protected_paths:
            _safe_output_path(destination, protected_paths=protected_paths)
            _reject_symlink_components(destination.parent, label="manifest output parent")
            if _output_identity(destination) != initial_identity:
                raise LocationManifestError(
                    f"manifest output changed during atomic replacement: {destination}"
                )
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _environment_file(
    path: Path,
    *,
    label: str = "TEST environment file",
    reject_duplicates: bool = False,
    reject_symlinks: bool = False,
) -> dict[str, str]:
    resolved = _resolve_existing(path, label=label, reject_symlinks=reject_symlinks)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != 0o600:
        raise LocationManifestError(f"{label} must have mode 0600: {resolved}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise LocationManifestError(f"invalid environment assignment at {resolved}:{line_number}")
        if reject_duplicates and name in values:
            raise LocationManifestError(
                f"duplicate environment assignment at {resolved}:{line_number}"
            )
        try:
            tokens = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise LocationManifestError(
                f"invalid environment value at {resolved}:{line_number}: {exc}"
            ) from exc
        if len(tokens) > 1:
            raise LocationManifestError(
                f"environment value must resolve to one token at {resolved}:{line_number}"
            )
        values[name] = tokens[0] if tokens else ""
    return values


def load_test_configuration(env_file: Path = TEST_ENV_FILE) -> tuple[Path, str, dict[str, str]]:
    """Load and fail-closed validate the fixed service-host TEST environment."""
    resolved_env = _resolve_existing(env_file, label="TEST environment file")
    expected_env = TEST_ENV_FILE.resolve()
    if resolved_env != expected_env:
        raise LocationManifestError(
            f"location manifest capture is TEST-only; environment file must be {expected_env}"
        )
    values = _environment_file(resolved_env)
    project_gid = _canonical_gid(
        values.get("DISH_COOKING_PROJECT_GID"), field="DISH_COOKING_PROJECT_GID"
    )
    if project_gid != TEST_COOKING_PROJECT_GID:
        raise LocationManifestError(
            "TEST environment project identity does not match the fixed TEST Cooking project"
        )
    raw_database = values.get("DISH_DB_PATH", "").strip()
    if not raw_database:
        raise LocationManifestError("TEST environment is missing DISH_DB_PATH")
    database = _resolve_existing(Path(raw_database), label="TEST SQLite authority database")
    try:
        database.relative_to(TEST_STATE_ROOT.resolve())
    except ValueError as exc:
        raise LocationManifestError(
            f"TEST database must remain under {TEST_STATE_ROOT.resolve()}: {database}"
        ) from exc
    if not values.get("ASANA_PAT", "").strip() and not values.get("ASANA_ENV", "").strip():
        raise LocationManifestError("TEST environment must define ASANA_PAT or ASANA_ENV")
    return database, project_gid, values


def _validate_production_credentials(values: Mapping[str, str]) -> tuple[str, Path]:
    inline = values.get("ASANA_PAT", "").strip()
    configured_path = values.get("ASANA_ENV", "").strip()
    if inline:
        raise LocationManifestError(
            "production environment must use the fixed Asana credential environment"
        )
    if not configured_path:
        raise LocationManifestError("production environment is missing ASANA_ENV")
    credential_file = _resolve_existing(
        Path(configured_path), label="production Asana credential environment", reject_symlinks=True
    )
    expected = PRODUCTION_ASANA_ENV_FILE.resolve()
    if credential_file != expected:
        raise LocationManifestError(
            f"production Asana credential environment must be {expected}"
        )
    credential_values = _environment_file(
        credential_file,
        label="production Asana credential environment",
        reject_duplicates=True,
        reject_symlinks=True,
    )
    asana_pat = credential_values.get("ASANA_PAT", "").strip()
    if not asana_pat:
        raise LocationManifestError("production Asana credential environment is missing ASANA_PAT")
    return asana_pat, credential_file


def load_production_configuration(
    env_file: Path = PRODUCTION_ENV_FILE,
) -> tuple[Path, str, str, Path, Path]:
    """Load the one fixed production service identity without TEST fallback."""
    resolved_env = _resolve_existing(
        env_file, label="production environment file", reject_symlinks=True
    )
    expected_env = PRODUCTION_ENV_FILE.resolve()
    if resolved_env != expected_env:
        raise LocationManifestError(
            f"production location manifest environment file must be {expected_env}"
        )
    if _same_file(resolved_env, TEST_ENV_FILE):
        raise LocationManifestError(
            "production environment file must not alias the TEST environment"
        )
    values = _environment_file(
        resolved_env,
        label="production environment file",
        reject_duplicates=True,
        reject_symlinks=True,
    )
    project_gid = _canonical_gid(
        values.get("DISH_COOKING_PROJECT_GID"), field="DISH_COOKING_PROJECT_GID"
    )
    if project_gid != PRODUCTION_COOKING_PROJECT_GID:
        raise LocationManifestError(
            "production environment project identity does not match the fixed production Cooking project"
        )
    raw_database = values.get("DISH_DB_PATH", "").strip()
    if not raw_database:
        raise LocationManifestError("production environment is missing DISH_DB_PATH")
    database = _resolve_existing(
        Path(raw_database), label="production SQLite authority database", reject_symlinks=True
    )
    expected_database = (PRODUCTION_STATE_ROOT / "shared.sqlite3").resolve()
    if database != expected_database:
        raise LocationManifestError(
            f"production database must be {expected_database}: {database}"
        )
    if _same_file(database, TEST_STATE_ROOT / "shared.sqlite3"):
        raise LocationManifestError(
            "production SQLite authority database must not alias the TEST database"
        )
    expected_identity_values = {
        "DISH_SERVICE_BACKUP_DIR": str(PRODUCTION_STATE_ROOT / "backups"),
        "DISH_SERVICE_PORT": "8775",
        "DISH_ACTION_PORT": "8776",
        "DISH_DARK_LAUNCH_SPOOL_PATH": str(
            PRODUCTION_STATE_ROOT / "dark-launch-spool.sqlite3"
        ),
        "DISH_DARK_LAUNCH_EMERGENCY_DIR": str(
            PRODUCTION_STATE_ROOT / "dark-launch-emergency"
        ),
        "DISH_DARK_LAUNCH_KILL_SWITCH": str(
            PRODUCTION_STATE_ROOT / "dark-launch.disabled"
        ),
    }
    for name, expected in expected_identity_values.items():
        actual = values.get(name, "").strip()
        if actual and actual != expected:
            raise LocationManifestError(
                f"production environment {name} does not match the fixed production identity"
            )
    profile = values.get("DISH_PROFILE", "").strip().lower()
    if profile and profile not in {"prod", "production"}:
        raise LocationManifestError(
            "production environment DISH_PROFILE does not identify production"
        )
    asana_pat, credential_file = _validate_production_credentials(values)
    return (
        database,
        project_gid,
        asana_pat,
        credential_file,
        PRODUCTION_STATE_ROOT / "dark-launch-spool.sqlite3",
    )


@contextmanager
def _asana_environment(values: Mapping[str, str]) -> Iterator[None]:
    keys = ("ASANA_PAT", "ASANA_ENV")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            value = values.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def production_task_reader() -> Iterator[Callable[[str], Mapping[str, Any]]]:
    """Expose one production Asana operation: exact task read by GID."""
    try:
        import asana
        from urllib3.util import Retry

        from dish_tool.backend import close_asana_sdk_client, load_asana_pat
        from dish_tool.constants import ASANA_REQUEST_TIMEOUT
    except ImportError as exc:
        raise LocationManifestError("production Asana read client is unavailable") from exc

    configuration = asana.Configuration()
    configuration.access_token = load_asana_pat()
    configuration.return_page_iterator = False
    configuration.retry_strategy = Retry(total=0, connect=0, read=0, redirect=0)
    api_client = asana.ApiClient(configuration)
    get_task = asana.TasksApi(api_client).get_task

    def read_task(task_gid: str) -> Mapping[str, Any]:
        try:
            response = get_task(
                task_gid,
                {"opt_fields": _ASANA_OPT_FIELDS},
                _request_timeout=ASANA_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            raise LocationManifestError(
                f"production Asana task read failed for task {task_gid}"
            ) from exc
        if not isinstance(response, Mapping) or "data" not in response:
            raise LocationManifestError(
                f"production Asana returned malformed task envelope for task {task_gid}"
            )
        data = response["data"]
        if not isinstance(data, Mapping):
            raise LocationManifestError(
                f"production Asana returned malformed task data for task {task_gid}"
            )
        return data

    try:
        yield read_task
    finally:
        close_asana_sdk_client(api_client)


def capture_test_location_manifest(*, env_file: Path, output: Path) -> int:
    database, project_gid, values = load_test_configuration(env_file)
    from dish_tool.backend import AsanaBackend
    from dish_tool.errors import DishRuleError

    try:
        with _asana_environment(values), AsanaBackend() as backend:
            manifest = build_location_manifest(
                database=database,
                project_gid=project_gid,
                read_task=backend.read_task,
            )
    except DishRuleError as exc:
        raise LocationManifestError(f"Asana TEST snapshot failed: {exc}") from exc
    _atomic_json(output, manifest)
    return len(manifest["tasks"])


def capture_production_location_manifest(*, env_file: Path, output: Path) -> int:
    database, project_gid, asana_pat, credential_file, dark_launch_spool = (
        load_production_configuration(env_file)
    )
    protected = [
        env_file,
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
        dark_launch_spool,
        TEST_ENV_FILE,
        TEST_STATE_ROOT / "shared.sqlite3",
        TEST_STATE_ROOT / "shared.sqlite3-wal",
        TEST_STATE_ROOT / "shared.sqlite3-shm",
        TEST_STATE_ROOT / "shared.sqlite3-journal",
        TEST_STATE_ROOT / "dark-launch-spool.sqlite3",
        credential_file,
    ]
    protected_paths = tuple(protected)
    _safe_output_path(output, protected_paths=protected_paths)
    try:
        with _asana_environment({"ASANA_PAT": asana_pat}), production_task_reader() as read_task:
            manifest = build_location_manifest(
                database=database,
                project_gid=project_gid,
                read_task=read_task,
                environment="production",
                require_nonzero=True,
            )
    except LocationManifestError:
        raise
    except Exception as exc:
        raise LocationManifestError("production Asana snapshot failed") from exc
    _atomic_json(output, manifest, protected_paths=protected_paths)
    return len(manifest["tasks"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dish-pg-build-location-manifest",
        description=(
            "Capture the complete Asana location/completion manifest for an explicit legacy SQLite environment."
        ),
    )
    parser.add_argument(
        "--environment", choices=sorted(_CAPTURE_ENVIRONMENTS), default="test"
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.environment == "test":
            count = capture_test_location_manifest(
                env_file=args.env_file or TEST_ENV_FILE, output=args.output
            )
        else:
            count = capture_production_location_manifest(
                env_file=args.env_file or PRODUCTION_ENV_FILE, output=args.output
            )
    except (LocationManifestError, OSError) as exc:
        print(f"dish-pg-build-location-manifest: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "captured": count,
                "identity_scheme": IDENTITY_SCHEME,
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
