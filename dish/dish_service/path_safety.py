"""Filesystem identity checks for dark-launch authority boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


class PathIdentityError(ValueError):
    """Two configured paths resolve to the same filesystem object."""


class KillSwitchPathError(RuntimeError):
    """A kill-switch operation targeted an existing non-marker file."""


_KILL_SWITCH_KIND = "dish-dark-launch-kill-switch"
_KILL_SWITCH_SCHEMA_VERSION = 1


def _normalized(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def paths_alias(left: Path, right: Path) -> bool:
    """Return true for equal normalized paths or existing aliases/hard links."""
    left_path, right_path = _normalized(left), _normalized(right)
    if left_path == right_path:
        return True
    try:
        return (
            left_path.exists()
            and right_path.exists()
            and os.path.samefile(left_path, right_path)
        )
    except OSError:
        return False


def require_distinct_paths(paths: Mapping[str, Path | None]) -> None:
    """Reject pairwise path aliasing with names suitable for operator errors."""
    values = [(name, Path(path)) for name, path in paths.items() if path is not None]
    for index, (left_name, left_path) in enumerate(values):
        for right_name, right_path in values[index + 1 :]:
            if paths_alias(left_path, right_path):
                raise PathIdentityError(
                    f"{left_name} and {right_name} must refer to distinct filesystem objects"
                )


def _validated_kill_switch(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KillSwitchPathError(
            f"refusing to treat existing non-marker file as a dark-launch kill switch: {path}"
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("kind") != _KILL_SWITCH_KIND
        or payload.get("schema_version") != _KILL_SWITCH_SCHEMA_VERSION
        or payload.get("disabled") is not True
    ):
        raise KillSwitchPathError(
            f"refusing to treat existing non-marker file as a dark-launch kill switch: {path}"
        )
    return payload


def inspect_kill_switch(path: Path) -> dict[str, Any]:
    """Classify a kill-switch path without creating, removing, or rewriting it."""
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        return {"state": "invalid", "path": str(target), "reason": "kill switch is a symlink"}
    try:
        payload = _validated_kill_switch(target)
    except FileNotFoundError:
        return {"state": "clear", "path": str(target), "reason": "marker is absent"}
    except KillSwitchPathError as exc:
        return {"state": "invalid", "path": str(target), "reason": str(exc)}
    return {
        "state": "engaged",
        "path": str(target),
        "reason": "validated disable marker is present",
        "marker_kind": payload.get("kind"),
        "schema_version": payload.get("schema_version"),
    }


def engage_kill_switch(path: Path, payload: Mapping[str, Any]) -> bool:
    """Create a durable marker without replacing any existing filesystem object.

    Returns ``True`` when this call created the marker and ``False`` when a
    valid marker already existed.  An existing database, spool, symlink, or
    unrelated file is never overwritten.
    """
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        raise KillSwitchPathError(f"dark-launch kill switch must not be a symlink: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = dict(payload)
    marker.update({
        "kind": _KILL_SWITCH_KIND,
        "schema_version": _KILL_SWITCH_SCHEMA_VERSION,
        "disabled": True,
    })
    body = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _validated_kill_switch(target)
        return False
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except BaseException:
        # Existence itself is fail-safe, even if a crash leaves a partial
        # marker.  Do not unlink it automatically and accidentally re-enable.
        raise


def clear_kill_switch(path: Path) -> bool:
    """Remove only a validated dark-launch marker, never an arbitrary file."""
    target = Path(path).expanduser().absolute()
    if target.is_symlink():
        raise KillSwitchPathError(f"dark-launch kill switch must not be a symlink: {target}")
    try:
        _validated_kill_switch(target)
    except FileNotFoundError:
        return False
    target.unlink()
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True
