"""Capture a TEST-only Asana location manifest for the legacy PostgreSQL export.

The legacy exporter deliberately has no Asana credential. This module performs
that separate observation step and emits exactly one location record for every
``task_content_state.task_gid`` in the TEST SQLite authority database.
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

TEST_ENV_FILE = Path("/home/marco/.config/dish-service/test.env")
TEST_STATE_ROOT = Path("/home/marco/.local/state/dish/test")
TEST_COOKING_PROJECT_GID = "1216693403164366"
IDENTITY_SCHEME = "dish-asana-gid-uuid5-v1"
# uuid5(uuid.NAMESPACE_URL, "urn:dish:identity:asana:v1")
ASANA_IDENTITY_NAMESPACE = uuid.UUID("a8ad7ec4-ec82-5764-b89e-1fed9c62e4a1")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENTITY_KINDS = frozenset({"task", "project", "section"})


class LocationManifestError(ValueError):
    """A fail-closed manifest capture or TEST-environment validation error."""


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
    return uuid.uuid5(ASANA_IDENTITY_NAMESPACE, f"{entity_kind}:{gid}")


def _resolve_existing(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LocationManifestError(f"{label} is unavailable: {path.expanduser()}: {exc}") from exc


def _task_gids(database: Path) -> tuple[str, ...]:
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
    return tuple(_canonical_gid(row[0], field="task_content_state.task_gid") for row in rows)


def _required_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocationManifestError(f"Asana returned malformed {field}")
    return value


def _project_section(task: Mapping[str, Any], project_gid: str) -> str:
    memberships = task.get("memberships")
    if not isinstance(memberships, list):
        raise LocationManifestError("Asana returned malformed task memberships")
    sections: set[str] = set()
    matching_memberships = 0
    for raw_membership in memberships:
        membership = _required_mapping(raw_membership, field="task membership")
        project = _required_mapping(membership.get("project"), field="membership project")
        if str(project.get("gid") or "").strip() != project_gid:
            continue
        matching_memberships += 1
        section = _required_mapping(membership.get("section"), field="membership section")
        sections.add(_canonical_gid(section.get("gid"), field="membership.section.gid"))
    if matching_memberships == 0:
        raise LocationManifestError(f"task is not a member of TEST project {project_gid}")
    if matching_memberships != 1 or len(sections) != 1:
        raise LocationManifestError(
            f"task has ambiguous placement in TEST project {project_gid}: sections={sorted(sections)}"
        )
    return next(iter(sections))


def build_location_manifest(
    *,
    database: Path,
    project_gid: str,
    read_task: Callable[[str], Mapping[str, Any]],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, object]:
    """Read every SQLite task from Asana and build an exact corpus manifest."""
    canonical_project_gid = _canonical_gid(project_gid, field="DISH_COOKING_PROJECT_GID")
    tasks: dict[str, dict[str, object]] = {}
    for task_gid in _task_gids(database):
        task = _required_mapping(read_task(task_gid), field=f"task {task_gid}")
        response_gid = _canonical_gid(task.get("gid"), field="task.gid")
        if response_gid != task_gid:
            raise LocationManifestError(
                f"Asana task identity mismatch requested={task_gid} returned={response_gid}"
            )
        completed = task.get("completed")
        if not isinstance(completed, bool):
            raise LocationManifestError(f"task {task_gid} completed must be a boolean")
        section_gid = _project_section(task, canonical_project_gid)
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
            "completed": completed,
            "observed_at": observed_at.isoformat(),
            "existence_state": "ordinary",
        }
    return {"tasks": tasks}


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _environment_file(path: Path) -> dict[str, str]:
    resolved = _resolve_existing(path, label="TEST environment file")
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode != 0o600:
        raise LocationManifestError(f"TEST environment file must have mode 0600: {resolved}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise LocationManifestError(
                f"invalid environment assignment at {resolved}:{line_number}"
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dish-pg-build-location-manifest",
        description=(
            "Capture the complete TEST Asana location/completion manifest for the TEST legacy SQLite corpus."
        ),
    )
    parser.add_argument("--env-file", type=Path, default=TEST_ENV_FILE)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        count = capture_test_location_manifest(env_file=args.env_file, output=args.output)
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
