"""Fail-closed file fence for the pre-cutover SQLite/Asana writer.

Presence of the configured file disables every legacy POST after authentication
and before request-body parsing. A malformed, replaced, or unreadable file still
fences the writer; it never reopens mutation authority by accident.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_tool.errors import DishRuleError


LEGACY_WRITER_FENCE_PROBE_PLAN = "authenticated POST rejected before body parsing"


@dataclass(frozen=True)
class WriterFenceObservation:
    """One internally consistent descriptor-based observation of the fence."""

    expected_path: str
    observed_path: str
    artifact_sha256: str
    manifest_sha256: str
    device: int | None
    inode: int | None
    size: int
    observed_at: datetime

    def as_evidence_payload(self) -> dict[str, Any]:
        return {
            "format": "dish-writer-fence-observation-v1",
            "expected_path": self.expected_path,
            "observed_path": self.observed_path,
            "exists": True,
            "regular_file": True,
            "symlink_free": True,
            "artifact_sha256": self.artifact_sha256,
            "manifest_sha256": self.manifest_sha256,
            "filesystem_device": self.device,
            "filesystem_inode": self.inode,
            "observed_size": self.size,
            "stable": True,
            "identity_match": True,
            "observed_at": self.observed_at.isoformat(),
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(manifest))).hexdigest()


def _normalized_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def planned_legacy_writer_fence_manifest(
    path: Path,
    *,
    fence_id: str,
    candidate_id: str,
    source_release: str,
    source_commit: str,
) -> dict[str, Any]:
    """Build the immutable manifest prepared in PostgreSQL and deployed on disk."""

    return {
        "format": "dish-legacy-writer-fence-v2",
        "fence_id": fence_id,
        "candidate_id": candidate_id,
        "path": str(_normalized_path(path)),
        "service_release": source_release,
        "source_commit": source_commit,
        "probe_plan": LEGACY_WRITER_FENCE_PROBE_PLAN,
    }


def _open_parent_directory(path: Path) -> tuple[int, str]:
    """Open every directory component without following symlinks."""

    normalized = _normalized_path(path)
    parts = normalized.parts
    if len(parts) < 2 or not normalized.name:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "writer fence path must name a file",
            rule="legacy_writer_fence_path_invalid",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parts[0], flags)
    try:
        for component in parts[1:-1]:
            try:
                next_fd = os.open(component, flags | nofollow, dir_fd=directory_fd)
            except OSError as exc:
                raise DishRuleError(
                    "CONFLICT",
                    "writer fence path contains an unavailable or symbolic-link directory",
                    rule="legacy_writer_fence_symlink_forbidden",
                    details={"path": str(normalized), "component": component},
                ) from exc
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd, parts[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def _signature(value: os.stat_result) -> tuple[int | None, int | None, int, int, int | None, int | None]:
    return (
        getattr(value, "st_dev", None),
        getattr(value, "st_ino", None),
        stat.S_IFMT(value.st_mode),
        value.st_size,
        getattr(value, "st_mtime_ns", None),
        getattr(value, "st_ctime_ns", None),
    )


def _read_stable_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    normalized = _normalized_path(path)
    parent_fd, name = _open_parent_directory(normalized)
    file_fd: int | None = None
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise DishRuleError(
                "NOT_FOUND",
                "legacy writer fence artifact is absent",
                rule="legacy_writer_fence_absent",
                details={"path": str(normalized)},
            ) from exc
        if stat.S_ISLNK(before.st_mode):
            raise DishRuleError(
                "CONFLICT",
                "legacy writer fence artifact must not be a symbolic link",
                rule="legacy_writer_fence_symlink_forbidden",
                details={"path": str(normalized)},
            )
        if not stat.S_ISREG(before.st_mode):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "legacy writer fence artifact is not a regular file",
                rule="legacy_writer_fence_type_invalid",
                details={"path": str(normalized)},
            )
        try:
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise DishRuleError(
                "CONFLICT",
                "legacy writer fence artifact could not be opened without following links",
                rule="legacy_writer_fence_symlink_forbidden",
                details={"path": str(normalized)},
            ) from exc
        opened = os.fstat(file_fd)
        if _signature(before) != _signature(opened):
            raise DishRuleError(
                "CONFLICT",
                "legacy writer fence artifact changed while it was opened",
                rule="legacy_writer_fence_observation_unstable",
                details={"path": str(normalized)},
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(file_fd)
        after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _signature(opened) != _signature(after_fd)
            or _signature(after_fd) != _signature(after_path)
        ):
            raise DishRuleError(
                "CONFLICT",
                "legacy writer fence artifact changed during observation",
                rule="legacy_writer_fence_observation_unstable",
                details={"path": str(normalized)},
            )
        return b"".join(chunks), after_fd
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def observe_legacy_writer_fence(
    path: Path,
    *,
    expected_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_artifact_sha256: str | None = None,
    expected_device: int | None = None,
    expected_inode: int | None = None,
    expected_size: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> WriterFenceObservation:
    """Observe and validate the exact on-disk fence artifact.

    The returned payload is intentionally shaped as a narrow service boundary.
    Agent A's durable evidence adapter can persist these values without the
    filesystem code depending on its ORM model.
    """

    observed_path = _normalized_path(path)
    planned_path = _normalized_path(expected_path or path)
    if observed_path != planned_path:
        raise DishRuleError(
            "CONFLICT",
            "legacy writer fence path differs from the planned artifact",
            rule="legacy_writer_fence_path_mismatch",
            details={"planned_path": str(planned_path), "observed_path": str(observed_path)},
        )
    raw, file_stat = _read_stable_regular_file(observed_path)
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "legacy writer fence artifact is not a valid manifest object",
            rule="legacy_writer_fence_manifest_invalid",
            details={"path": str(observed_path), "error_type": type(exc).__name__},
        ) from exc
    if not isinstance(manifest, dict):
        raise DishRuleError(
            "VALIDATION_FAILED",
            "legacy writer fence manifest must be an object",
            rule="legacy_writer_fence_manifest_invalid",
            details={"path": str(observed_path)},
        )
    artifact_digest = hashlib.sha256(raw).hexdigest()
    manifest_digest = manifest_sha256(manifest)
    identity = {
        "manifest_sha256": (expected_manifest_sha256, manifest_digest),
        "artifact_sha256": (expected_artifact_sha256, artifact_digest),
        "filesystem_device": (expected_device, getattr(file_stat, "st_dev", None)),
        "filesystem_inode": (expected_inode, getattr(file_stat, "st_ino", None)),
        "observed_size": (expected_size, file_stat.st_size),
    }
    mismatches = {
        field: {"planned": planned, "observed": observed}
        for field, (planned, observed) in identity.items()
        if planned is not None and planned != observed
    }
    if mismatches:
        raise DishRuleError(
            "CONFLICT",
            "legacy writer fence artifact identity differs from the planned artifact",
            rule="legacy_writer_fence_identity_mismatch",
            details={"path": str(observed_path), "mismatches": mismatches},
        )
    observed_at = clock() if clock is not None else datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("writer fence observation clock must return an aware timestamp")
    return WriterFenceObservation(
        expected_path=str(planned_path),
        observed_path=str(observed_path),
        artifact_sha256=artifact_digest,
        manifest_sha256=manifest_digest,
        device=getattr(file_stat, "st_dev", None),
        inode=getattr(file_stat, "st_ino", None),
        size=file_stat.st_size,
        observed_at=observed_at.astimezone(timezone.utc),
    )


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def read_legacy_writer_fence(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not os.path.lexists(path):
        return None, None
    try:
        raw, _file_stat = _read_stable_regular_file(path)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs)
        if not isinstance(value, dict):
            raise ValueError("fence manifest must be an object")
        return value, manifest_sha256(value)
    except Exception as exc:
        # Corrupt or unsafe evidence is still an engaged fence. Callers receive
        # a stable classification without following a link or exposing content.
        return {
            "format": "dish-legacy-writer-fence-unreadable-v1",
            "error_type": type(exc).__name__,
        }, hashlib.sha256(b"").hexdigest()


def engage_legacy_writer_fence(
    path: Path,
    *,
    fence_id: str,
    candidate_id: str,
    source_release: str,
    source_commit: str,
    engaged_at: datetime,
    operator: str,
) -> tuple[dict[str, Any], str]:
    if engaged_at.tzinfo is None:
        raise ValueError("writer fence engagement timestamp must be timezone-aware")
    if not operator.strip():
        raise ValueError("writer fence operator must be nonblank")
    manifest = planned_legacy_writer_fence_manifest(
        path,
        fence_id=fence_id,
        candidate_id=candidate_id,
        source_release=source_release,
        source_commit=source_commit,
    )
    digest = manifest_sha256(manifest)
    existing, existing_digest = read_legacy_writer_fence(path)
    if existing is not None:
        if existing_digest != digest:
            raise DishRuleError(
                "CONFLICT",
                "a different legacy writer fence is already engaged",
                rule="legacy_writer_fence_conflict",
                details={"existing_sha256": existing_digest},
            )
        return existing, digest

    path = _normalized_path(path)
    # Deployment owns the governed directory. Open every existing parent
    # component without following links before any filesystem mutation.
    parent_fd, name = _open_parent_directory(path)
    temp_name = f".{name}.uncommitted.{secrets.token_hex(8)}"
    file_fd: int | None = None
    try:
        file_fd = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(file_fd, "wb") as handle:
            file_fd = None
            handle.write(_canonical(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(
                temp_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing, existing_digest = read_legacy_writer_fence(path)
            if existing_digest != digest:
                raise DishRuleError(
                    "CONFLICT",
                    "a different legacy writer fence appeared during engagement",
                    rule="legacy_writer_fence_conflict",
                    details={"existing_sha256": existing_digest},
                )
            return existing, digest
        os.fsync(parent_fd)
        observe_legacy_writer_fence(path, expected_manifest_sha256=digest)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)
    return manifest, digest


def release_legacy_writer_fence(path: Path, *, expected_sha256: str) -> None:
    if not os.path.lexists(path):
        return
    try:
        observation = observe_legacy_writer_fence(
            path, expected_manifest_sha256=expected_sha256
        )
    except DishRuleError as exc:
        if exc.rule == "legacy_writer_fence_identity_mismatch":
            actual = (
                exc.details.get("mismatches", {})
                .get("manifest_sha256", {})
                .get("observed")
            )
            raise DishRuleError(
                "CONFLICT",
                "legacy writer fence digest does not match",
                rule="legacy_writer_fence_release_conflict",
                details={"actual_sha256": actual},
            ) from exc
        raise
    normalized = _normalized_path(path)
    parent_fd, name = _open_parent_directory(normalized)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            getattr(current, "st_dev", None) != observation.device
            or getattr(current, "st_ino", None) != observation.inode
            or current.st_size != observation.size
        ):
            raise DishRuleError(
                "CONFLICT",
                "legacy writer fence changed before release",
                rule="legacy_writer_fence_observation_unstable",
                details={"path": str(normalized)},
            )
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def assert_legacy_writer_mutation_allowed(path: Path | None) -> None:
    if path is None or not os.path.lexists(path):
        return
    _manifest, digest = read_legacy_writer_fence(path)
    raise DishRuleError(
        "CONFLICT",
        "legacy mutation authority is mechanically fenced for PostgreSQL cutover",
        rule="legacy_writer_fenced",
        retryable=False,
        details={"fence_sha256": digest},
    )
