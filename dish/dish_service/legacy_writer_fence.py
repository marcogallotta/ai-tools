"""Fail-closed file fence for the pre-cutover SQLite/Asana writer.

Presence of the configured file disables every legacy POST after authentication
and before request-body parsing.  A malformed or unreadable file still fences the
writer; it never reopens mutation authority by accident.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from dish_tool.errors import DishRuleError


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(manifest))).hexdigest()


def read_legacy_writer_fence(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("fence manifest must be an object")
        return value, hashlib.sha256(_canonical(value)).hexdigest()
    except Exception as exc:
        # Corrupt evidence is still an engaged fence.  Callers receive a stable
        # classification without exposing file contents.
        return {
            "format": "dish-legacy-writer-fence-unreadable-v1",
            "error_type": type(exc).__name__,
        }, hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


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
    manifest = {
        "format": "dish-legacy-writer-fence-v1",
        "fence_id": fence_id,
        "candidate_id": candidate_id,
        "source_release": source_release,
        "source_commit": source_commit,
        "engaged_at": engaged_at.isoformat(),
        "operator": operator,
    }
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

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(manifest) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return manifest, digest


def release_legacy_writer_fence(path: Path, *, expected_sha256: str) -> None:
    manifest, digest = read_legacy_writer_fence(path)
    if manifest is None:
        return
    if digest != expected_sha256:
        raise DishRuleError(
            "CONFLICT",
            "legacy writer fence digest does not match",
            rule="legacy_writer_fence_release_conflict",
            details={"actual_sha256": digest},
        )
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def assert_legacy_writer_mutation_allowed(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    _manifest, digest = read_legacy_writer_fence(path)
    raise DishRuleError(
        "CONFLICT",
        "legacy mutation authority is mechanically fenced for PostgreSQL cutover",
        rule="legacy_writer_fenced",
        retryable=False,
        details={"fence_sha256": digest},
    )
