"""Committed protocol-release resolution."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .constants import (
    GOVERNED_RELEASE_FILENAMES,
    MANIFEST_FILENAMES,
    PROTOCOL_FILENAMES,
    RELEASE_VERSION_FILENAME,
)
from .errors import ReleaseResolutionError
from .models import ResolvedRelease
from .validation import validate_manifest_shape

_RELEASE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _git(
    repo: Path, *args: str, check: bool = True, preserve_output: bool = False
) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise ReleaseResolutionError(
            "release_git_error",
            "unable to inspect protocol release Git worktree",
            stderr=stderr.strip(),
        ) from exc
    return completed.stdout if preserve_output else completed.stdout.strip()


def _find_release_file(git_root: Path) -> Path:
    matches = [
        path
        for path in git_root.rglob(RELEASE_VERSION_FILENAME)
        if ".git" not in path.parts and path.is_file()
    ]
    if not matches:
        raise ReleaseResolutionError(
            "release_missing", f"missing {RELEASE_VERSION_FILENAME}"
        )
    if len(matches) != 1:
        raise ReleaseResolutionError(
            "release_ambiguous",
            f"found {len(matches)} {RELEASE_VERSION_FILENAME} files",
            paths=[str(path.relative_to(git_root)) for path in matches],
        )
    return matches[0]


def resolve_release(worktree: str | os.PathLike[str]) -> ResolvedRelease:
    requested = Path(worktree).expanduser().resolve()
    if not requested.exists():
        raise ReleaseResolutionError(
            "release_missing", f"protocol worktree does not exist: {requested}"
        )
    try:
        git_root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    except ReleaseResolutionError as exc:
        raise ReleaseResolutionError(
            "release_git_error", "protocol release path is not a Git worktree"
        ) from exc

    release_file = _find_release_file(git_root)
    release_root = release_file.parent
    required_paths = [release_file] + [
        release_root / filename for filename in GOVERNED_RELEASE_FILENAMES
    ]
    missing = [path.name for path in required_paths if not path.is_file()]
    if missing:
        raise ReleaseResolutionError(
            "release_incomplete", "protocol release is incomplete", files=missing
        )

    relative_paths = [str(path.relative_to(git_root)) for path in required_paths]
    dirty = _git(git_root, "status", "--porcelain", "--", *relative_paths)
    if dirty:
        raise ReleaseResolutionError(
            "release_dirty",
            "protocol release has uncommitted or untracked changes",
            status=dirty.splitlines(),
        )
    for relative in relative_paths:
        try:
            _git(git_root, "ls-files", "--error-unmatch", "--", relative)
        except ReleaseResolutionError as exc:
            raise ReleaseResolutionError(
                "release_incomplete",
                "protocol release contains an untracked governed file",
                file=relative,
            ) from exc

    release_relative = str(release_file.relative_to(git_root))
    release_commits = _git(
        git_root, "log", "--format=%H", "--", release_relative
    ).splitlines()
    if not release_commits:
        raise ReleaseResolutionError(
            "release_incomplete", "protocol_release has no commit binding"
        )
    release_commit = release_commits[0]
    previous_release_commit = release_commits[1] if len(release_commits) > 1 else None

    governed_relative = [
        str((release_root / filename).relative_to(git_root))
        for filename in GOVERNED_RELEASE_FILENAMES
    ]
    later_governed_commits = _git(
        git_root,
        "log",
        "--format=%H",
        f"{release_commit}..HEAD",
        "--",
        *governed_relative,
    )
    if later_governed_commits:
        raise ReleaseResolutionError(
            "release_commit_mismatch",
            "governed files changed after the current protocol_release commit",
            commits=later_governed_commits.splitlines(),
        )
    if previous_release_commit is None:
        initial_commits = {
            _git(git_root, "log", "-1", "--format=%H", "--", relative)
            for relative in governed_relative
        }
        if initial_commits != {release_commit}:
            raise ReleaseResolutionError(
                "release_commit_mismatch",
                "the initial governed release was not committed atomically",
                commits=sorted(initial_commits),
            )
    else:
        release_window_commits = set(
            _git(
                git_root,
                "log",
                "--format=%H",
                f"{previous_release_commit}..{release_commit}",
                "--",
                *governed_relative,
            ).splitlines()
        )
        if not release_window_commits <= {release_commit}:
            raise ReleaseResolutionError(
                "release_commit_mismatch",
                "governed files changed before the wrapper advanced atomically",
                commits=sorted(release_window_commits),
            )
    for relative in relative_paths:
        try:
            _git(git_root, "cat-file", "-e", f"{release_commit}:{relative}")
        except ReleaseResolutionError as exc:
            raise ReleaseResolutionError(
                "release_incomplete",
                "a governed file did not exist at the protocol_release commit",
                file=relative,
            ) from exc

    committed_text = {
        path.name: _git(
            git_root,
            "show",
            f"{release_commit}:{path.relative_to(git_root).as_posix()}",
            preserve_output=True,
        )
        for path in required_paths
    }
    version = committed_text[RELEASE_VERSION_FILENAME].strip()
    if not version or not _RELEASE_VERSION_RE.fullmatch(version):
        raise ReleaseResolutionError(
            "release_malformed", "protocol_release contains an invalid version"
        )
    if previous_release_commit is not None:
        previous_version = _git(
            git_root,
            "show",
            f"{previous_release_commit}:{release_relative}",
            preserve_output=True,
        ).strip()
        if previous_version == version:
            raise ReleaseResolutionError(
                "release_version_not_advanced",
                "protocol_release version was reused instead of advanced",
                version=version,
            )

    protocols: dict[str, str] = {}
    for role, filename in PROTOCOL_FILENAMES.items():
        content = committed_text[filename]
        if not content.strip():
            raise ReleaseResolutionError(
                "release_incomplete", f"protocol file is empty: {filename}"
            )
        protocols[role] = content

    manifests: dict[str, dict[str, Any]] = {}
    manifest_texts: dict[str, str] = {}
    for kind, filename in MANIFEST_FILENAMES.items():
        raw = committed_text[filename]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReleaseResolutionError(
                "manifest_malformed",
                f"invalid JSON in {filename}",
                line=exc.lineno,
                column=exc.colno,
            ) from exc
        parsed = validate_manifest_shape(parsed, expected_kind=kind, filename=filename)
        if parsed.get("protocol_release") != version:
            raise ReleaseResolutionError(
                "release_version_mismatch",
                f"{filename} is not bound to protocol_release {version}",
            )
        manifests[kind] = parsed
        manifest_texts[kind] = raw

    return ResolvedRelease(
        version=version,
        commit=release_commit,
        root=release_root,
        protocols=copy.deepcopy(protocols),
        manifests=copy.deepcopy(manifests),
        manifest_texts=copy.deepcopy(manifest_texts),
    )
