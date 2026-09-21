"""Immutable production release staging and exact-pointer rollback control."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = ".dish-release.json"
MANIFEST_VERSION = 1
DEFAULT_RELEASE_ROOT = Path("/home/marco/.local/share/dish/releases")
DEFAULT_CURRENT = Path("/home/marco/.local/share/dish/prod-current")
DEFAULT_PREVIOUS = Path("/home/marco/.local/share/dish/prod-previous")
DEFAULT_ENV_FILE = Path("/home/marco/.config/dish-service/prod.env")
DEFAULT_HEALTH_URL = "http://127.0.0.1:8775/health"
DEFAULT_UNIT = "dish-service-prod.service"
_SHA = re.compile(r"[0-9a-f]{40}")
_SCHEMA_ASSIGNMENT = re.compile(
    r'^ALEMBIC_HEAD\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE
)
_HASHED_PATHS = (
    "dish/dish-service",
    "dish/dish_pg/schema_identity.py",
    "dish/requirements.txt",
)


class ReleaseError(RuntimeError):
    """A release cannot be proven safe to activate."""


@contextmanager
def _release_lock(release_root: Path):
    release_root.mkdir(parents=True, exist_ok=True)
    with (release_root / ".prod-release.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


@dataclass(frozen=True)
class Release:
    root: Path
    source_commit: str
    schema_head: str
    hashes: Mapping[str, str]

    @property
    def dish_root(self) -> Path:
        return self.root / "dish"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    excluded_parts = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if excluded_parts.intersection(relative.parts):
            continue
        if relative.name == MANIFEST_NAME or relative.name.startswith(
            f".{MANIFEST_NAME}."
        ):
            continue
        if not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _installed_packages_sha256(python: Path) -> str:
    try:
        completed = subprocess.run(
            [str(python), "-m", "pip", "freeze", "--all"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ReleaseError("cannot inspect release dependencies") from exc
    if completed.returncode != 0:
        raise ReleaseError("cannot inspect release dependencies")
    normalized = "\n".join(
        sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())
    )
    return hashlib.sha256((normalized + "\n").encode()).hexdigest()


def _schema_head(source: Path) -> str:
    match = _SCHEMA_ASSIGNMENT.search(source.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseError(f"cannot read ALEMBIC_HEAD from {source}")
    return match.group(1)


def _exact_commit(repository: Path, revision: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            f"{revision}^{{commit}}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or _SHA.fullmatch(value) is None:
        raise ReleaseError("revision does not resolve to an exact commit")
    return value


def _manifest_payload(release_root: Path, source_commit: str) -> dict[str, object]:
    dish_root = release_root / "dish"
    python = dish_root / ".venv/bin/python"
    return {
        "version": MANIFEST_VERSION,
        "source_commit": source_commit,
        "schema_head": _schema_head(dish_root / "dish_pg/schema_identity.py"),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "hashes": {name: _sha256(release_root / name) for name in _HASHED_PATHS},
        "source_tree_sha256": _source_tree_sha256(release_root),
        "installed_packages_sha256": _installed_packages_sha256(python),
    }


def _write_manifest(release_root: Path, payload: Mapping[str, object]) -> None:
    destination = release_root / MANIFEST_NAME
    temporary = release_root / f".{MANIFEST_NAME}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, destination)


def stage_release(
    repository: Path,
    revision: str,
    release_root: Path,
    python_executable: Path | None = None,
) -> Release:
    """Export and build one immutable release, publishing it only when complete."""
    repository = repository.resolve()
    release_root.mkdir(parents=True, exist_ok=True)
    source_commit = _exact_commit(repository, revision)
    destination = release_root / source_commit
    if destination.exists() or destination.is_symlink():
        return load_release(release_root, source_commit)

    staging = Path(tempfile.mkdtemp(prefix=".dish-release-", dir=release_root))
    archive = staging.with_suffix(".tar")
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "archive",
                "--format=tar",
                f"--output={archive}",
                source_commit,
            ],
            check=True,
        )
        with tarfile.open(archive, mode="r:") as bundle:
            bundle.extractall(staging, filter="data")
        dish_root = staging / "dish"
        python = dish_root / ".venv/bin/python"
        builder = python_executable or repository / "dish/.venv/bin/python"
        if not builder.is_file():
            builder = Path(sys.executable)
        subprocess.run(
            [str(builder), "-m", "venv", str(dish_root / ".venv")], check=True
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-r",
                str(dish_root / "requirements.txt"),
            ],
            check=True,
        )
        _write_manifest(staging, _manifest_payload(staging, source_commit))
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        archive.unlink(missing_ok=True)
    return load_release(release_root, source_commit)


def certify_existing_release(release_root: Path, source_commit: str) -> Release:
    """Adopt a pre-controller Git worktree only after proving its exact tracked identity."""
    if _SHA.fullmatch(source_commit) is None:
        raise ReleaseError("release identity must be an exact lowercase commit SHA")
    release_root = release_root.resolve()
    path = release_root / source_commit
    if path.is_symlink() or not path.is_dir() or path.parent != release_root:
        raise ReleaseError("release must be a real direct child of the release root")
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
        capture_output=True,
        check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != source_commit:
        raise ReleaseError(
            "existing release Git HEAD does not match its directory identity"
        )
    if status.returncode != 0 or status.stdout.strip():
        raise ReleaseError("existing release has modified or untracked files")
    if not (path / "dish/.venv/bin/python").is_file():
        raise ReleaseError("existing release virtual environment is missing")
    _write_manifest(path, _manifest_payload(path, source_commit))
    return load_release(release_root, source_commit)


def load_release(release_root: Path, source_commit: str) -> Release:
    """Validate directory identity, manifest identity, and release-critical bytes."""
    if _SHA.fullmatch(source_commit) is None:
        raise ReleaseError("release identity must be an exact lowercase commit SHA")
    release_root = release_root.resolve()
    path = release_root / source_commit
    if path.is_symlink() or not path.is_dir() or path.parent != release_root:
        raise ReleaseError("release must be a real direct child of the release root")
    try:
        payload = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError("release manifest is missing or invalid") from exc
    if (
        payload.get("version") != MANIFEST_VERSION
        or payload.get("source_commit") != source_commit
    ):
        raise ReleaseError("release manifest identity does not match its directory")
    schema_head = payload.get("schema_head")
    hashes = payload.get("hashes")
    tree_digest = payload.get("source_tree_sha256")
    packages_digest = payload.get("installed_packages_sha256")
    if (
        not isinstance(schema_head, str)
        or not schema_head
        or not isinstance(hashes, dict)
        or not isinstance(tree_digest, str)
        or not isinstance(packages_digest, str)
    ):
        raise ReleaseError("release manifest is incomplete")
    if _schema_head(path / "dish/dish_pg/schema_identity.py") != schema_head:
        raise ReleaseError("release schema identity differs from its manifest")
    for name in _HASHED_PATHS:
        expected = hashes.get(name)
        if not isinstance(expected, str) or _sha256(path / name) != expected:
            raise ReleaseError(f"release integrity check failed for {name}")
    if _source_tree_sha256(path) != tree_digest:
        raise ReleaseError("release source-tree integrity check failed")
    python = path / "dish/.venv/bin/python"
    if not python.is_file():
        raise ReleaseError("release virtual environment is missing")
    if _installed_packages_sha256(python) != packages_digest:
        raise ReleaseError("release dependency integrity check failed")
    return Release(path, source_commit, schema_head, dict(hashes))


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip():
            raise ReleaseError(f"invalid environment line in {path}")
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def preflight_database(release: Release, env_file: Path) -> None:
    values = _read_env(env_file)
    expected = values.get("DISH_PG_EXPECTED_SCHEMA_HEAD")
    if expected != release.schema_head:
        raise ReleaseError(
            f"release schema {release.schema_head!r} is incompatible with configured schema {expected!r}"
        )
    required = {
        "DISH_PG_DATABASE_URL",
        "DISH_PG_EXPECTED_DATABASE_NAME",
        "DISH_PG_EXPECTED_SCHEMA_HEAD",
        "DISH_PG_EXPECTED_RELEASE",
        "DISH_PG_EXPECTED_GENERATION_ID",
    }
    if missing := sorted(name for name in required if not values.get(name)):
        raise ReleaseError(
            "production identity environment is incomplete: " + ", ".join(missing)
        )
    candidate_env = os.environ.copy()
    candidate_env.update(values)
    completed = subprocess.run(
        [
            str(release.dish_root / ".venv/bin/python"),
            "-m",
            "dish_pg.release_preflight",
        ],
        cwd=release.dish_root,
        env=candidate_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseError(
            "release identity is incompatible with the live production authority"
        )


def _pointer_value(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_symlink():
        raise ReleaseError(f"{path} is not a symlink")
    return os.readlink(path)


def _set_pointer(path: Path, value: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    if value is None:
        path.unlink(missing_ok=True)
        return
    os.symlink(value, temporary)
    os.replace(temporary, path)


class SystemOperations:
    def __init__(self, unit: str, health_url: str, timeout: float) -> None:
        self.unit = unit
        self.health_url = health_url
        self.timeout = timeout

    def restart(self) -> None:
        subprocess.run(["sudo", "/usr/bin/systemctl", "restart", self.unit], check=True)

    def verify(self, release: Release) -> None:
        deadline = time.monotonic() + self.timeout
        last_error = "service did not become healthy"
        while time.monotonic() < deadline:
            try:
                active = subprocess.run(
                    ["/usr/bin/systemctl", "is-active", self.unit],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if active.stdout.strip() != "active":
                    raise ReleaseError("service is not active")
                pid_result = subprocess.run(
                    [
                        "/usr/bin/systemctl",
                        "show",
                        self.unit,
                        "--property=MainPID",
                        "--value",
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                )
                pid = int(pid_result.stdout.strip())
                if Path(f"/proc/{pid}/cwd").resolve() != release.dish_root.resolve():
                    raise ReleaseError(
                        "service process is not running from the selected release"
                    )
                with urllib.request.urlopen(self.health_url, timeout=2) as response:
                    payload = json.load(response)
                if payload.get("ok") is not True:
                    raise ReleaseError("service health is not ready")
                if payload.get("code_release") != release.source_commit:
                    raise ReleaseError(
                        "service health reports a different executable release"
                    )
                return
            except (
                OSError,
                ValueError,
                ReleaseError,
                subprocess.SubprocessError,
            ) as exc:  # bounded retry includes connection startup races
                last_error = str(exc)
                time.sleep(0.25)
        raise ReleaseError(last_error)


def activate_release(
    release: Release,
    *,
    current: Path,
    previous: Path,
    operations: SystemOperations,
    database_preflight: Callable[[Release], None],
) -> None:
    """Activate exactly one validated release, restoring both pointers on failure."""
    database_preflight(release)
    old_current = _pointer_value(current)
    old_previous = _pointer_value(previous)
    prior = None
    if old_current is not None:
        old_current_path = current.resolve()
        if old_current_path.parent != release.root.parent:
            raise ReleaseError("current release is outside the configured release root")
        prior = load_release(release.root.parent, old_current_path.name)
    if old_current is not None and current.resolve() == release.root.resolve():
        operations.verify(release)
        return
    try:
        if old_current is not None:
            _set_pointer(previous, old_current)
        _set_pointer(current, str(release.root))
        operations.restart()
        operations.verify(release)
    except Exception as activation_error:
        _set_pointer(current, old_current)
        _set_pointer(previous, old_previous)
        if old_current is not None:
            try:
                operations.restart()
                assert prior is not None
                operations.verify(prior)
            except Exception as recovery_error:
                raise ReleaseError(
                    f"activation failed ({activation_error}); prior release recovery also failed ({recovery_error})"
                ) from recovery_error
        raise ReleaseError(
            f"activation failed and prior pointer was restored: {activation_error}"
        ) from activation_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, default=DEFAULT_RELEASE_ROOT)
    parser.add_argument("--current", type=Path, default=DEFAULT_CURRENT)
    parser.add_argument("--previous", type=Path, default=DEFAULT_PREVIOUS)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--unit", default=DEFAULT_UNIT)
    parser.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--repository", type=Path, required=True)
    stage.add_argument("--revision", required=True)
    stage.add_argument("--python", type=Path)
    certify = commands.add_parser("certify-existing")
    certify.add_argument("release")
    activate = commands.add_parser("activate")
    activate.add_argument("release")
    commands.add_parser("rollback")
    commands.add_parser("status")
    return parser


def _run(args: argparse.Namespace) -> None:
    if args.command == "stage":
        release = stage_release(
            args.repository, args.revision, args.release_root, args.python
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "release": release.source_commit,
                    "path": str(release.root),
                }
            )
        )
        return
    if args.command == "status":
        print(
            json.dumps(
                {
                    "ok": True,
                    "current": _pointer_value(args.current),
                    "previous": _pointer_value(args.previous),
                }
            )
        )
        return
    if args.command == "certify-existing":
        release = certify_existing_release(args.release_root, args.release)
        print(json.dumps({"ok": True, "release": release.source_commit}))
        return
    previous_value = (
        _pointer_value(args.previous) if args.command == "rollback" else None
    )
    identity = (
        args.release if args.command == "activate" else Path(previous_value or "").name
    )
    if not identity:
        raise ReleaseError("no previous release is recorded; rollback cannot proceed")
    release = load_release(args.release_root, identity)
    if args.command == "rollback" and args.previous.resolve() != release.root.resolve():
        raise ReleaseError(
            "previous pointer does not resolve to its recorded release identity"
        )
    operations = SystemOperations(args.unit, args.health_url, args.timeout)
    activate_release(
        release,
        current=args.current,
        previous=args.previous,
        operations=operations,
        database_preflight=lambda candidate: preflight_database(
            candidate, args.env_file
        ),
    )
    print(json.dumps({"ok": True, "release": release.source_commit}))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with _release_lock(args.release_root):
            _run(args)
        return 0
    except (OSError, subprocess.SubprocessError, ReleaseError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


__all__ = [
    "MANIFEST_NAME",
    "Release",
    "ReleaseError",
    "SystemOperations",
    "activate_release",
    "certify_existing_release",
    "load_release",
    "main",
    "preflight_database",
    "stage_release",
]
