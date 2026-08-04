"""Narrow descriptor-bound verification for release evidence artifacts."""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from .release_evidence import ReleaseAuthorityError, _require_sha256


@dataclass(frozen=True)
class ReleaseArtifactObservation:
    canonical_path: str
    content_sha256: str
    size: int
    filesystem_device: int
    filesystem_inode: int
    mtime_ns: int


def _normalized_absolute_path(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseAuthorityError("release artifact path must be a nonblank absolute path")
    path = PurePosixPath(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ReleaseAuthorityError("release artifact path must be absolute and normalized")
    canonical = str(path)
    if canonical != value:
        raise ReleaseAuthorityError("release artifact path must use its canonical normalized spelling")
    return canonical, tuple(path.parts[1:])


def observe_release_artifact(
    *, artifact_path: object, expected_sha256: object
) -> ReleaseArtifactObservation:
    """Read one existing regular file without following any parent or final symlink."""
    expected = _require_sha256(expected_sha256, "artifact_sha256")
    canonical, components = _normalized_absolute_path(artifact_path)
    if not components:
        raise ReleaseAuthorityError("release artifact path must identify a file")

    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in components[:-1]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise ReleaseAuthorityError(
                    "release artifact parent is absent, not a directory, or contains a symlink"
                ) from exc
            os.close(directory_fd)
            directory_fd = next_fd

        try:
            file_fd = os.open(
                components[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ReleaseAuthorityError(
                "release artifact is absent, inaccessible, or a forbidden symlink"
            ) from exc
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ReleaseAuthorityError("release artifact must be a regular file")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(file_fd)
            stable = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) == (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if not stable:
                raise ReleaseAuthorityError("release artifact observation was unstable")
            observed = digest.hexdigest()
            if observed != expected:
                raise ReleaseAuthorityError("release artifact digest does not match recorded identity")
            return ReleaseArtifactObservation(
                canonical_path=canonical,
                content_sha256=observed,
                size=after.st_size,
                filesystem_device=after.st_dev,
                filesystem_inode=after.st_ino,
                mtime_ns=after.st_mtime_ns,
            )
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)
