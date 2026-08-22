from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .common import fail, now_utc, path_is_within

MARKER_SCHEMA = "dish-agent-worktree-tools-venv-v1"
MARKER_NAME = ".dish-agent-worktree-bootstrap.json"
CANONICAL_VENV_RELATIVE = Path("tools/.venv")
CANONICAL_REQUIREMENTS_RELATIVE = Path("tools/requirements.txt")


class ToolEnvironmentError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bootstrap_identity() -> dict[str, str]:
    executable = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    return {
        "executable": str(executable),
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }


def _effective_requirements_present(path: Path) -> bool:
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def _desired_identity(worktree: Path, head: str) -> dict[str, Any]:
    root = worktree.resolve()
    tools_dir = root / "tools"
    requirements = root / CANONICAL_REQUIREMENTS_RELATIVE
    if tools_dir.is_symlink() or not tools_dir.is_dir() or tools_dir.resolve().parent != root:
        raise ToolEnvironmentError("tracked tools/ directory is missing, redirected, or not worktree-local")
    if requirements.is_symlink() or not requirements.is_file() or not path_is_within(requirements.resolve(), root):
        raise ToolEnvironmentError("tracked tools/requirements.txt is missing, redirected, or not worktree-local")
    return {
        "schema": MARKER_SCHEMA,
        "worktree": str(root),
        "source_head": head,
        "requirements_sha256": _sha256(requirements),
        "bootstrap_sha256": _sha256(Path(__file__).resolve()),
        "bootstrap_interpreter": _bootstrap_identity(),
    }


def _read_marker(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _marker_rebuild_required(marker: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    if marker is None:
        return True
    for key in ("schema", "worktree", "requirements_sha256", "bootstrap_sha256", "bootstrap_interpreter"):
        if marker.get(key) != desired.get(key):
            return True
    return False


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no command output"
        raise ToolEnvironmentError(f"command failed ({completed.returncode}): {' '.join(command)}: {detail}")
    return completed


def _remove_venv(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _create_venv(worktree: Path, requirements: Path, venv: Path) -> None:
    _remove_venv(venv)
    command = [str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()), "-m", "venv"]
    if not _effective_requirements_present(requirements):
        command.append("--without-pip")
    command.append(str(venv))
    _run(command, cwd=worktree)
    if _effective_requirements_present(requirements):
        _run([str(venv / "bin/python"), "-m", "pip", "install", "-r", str(requirements)], cwd=worktree)


def _runtime_valid(worktree: Path, requirements: Path, venv: Path) -> bool:
    if venv.is_symlink() or not venv.is_dir():
        return False
    python = venv / "bin/python"
    if not python.exists():
        return False
    expected_prefix = str(venv.resolve())
    try:
        check = subprocess.run(
            [str(python), "-c", "import pathlib,sys; print(pathlib.Path(sys.prefix).resolve())"],
            cwd=worktree,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return False
    if check.returncode != 0 or check.stdout.strip() != expected_prefix:
        return False
    if not _effective_requirements_present(requirements):
        return True
    for command in (
        [str(python), "-m", "pip", "check"],
        [str(python), "-m", "pip", "install", "--dry-run", "--no-index", "-r", str(requirements)],
    ):
        try:
            completed = subprocess.run(
                command,
                cwd=worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError:
            return False
        if completed.returncode != 0:
            return False
    return True


def ensure_tool_environment(worktree: Path, head: str) -> dict[str, Any]:
    root = worktree.resolve()
    try:
        desired = _desired_identity(root, head)
        requirements = root / CANONICAL_REQUIREMENTS_RELATIVE
        venv = root / CANONICAL_VENV_RELATIVE
        marker_path = venv / MARKER_NAME
        marker = _read_marker(marker_path)
        rebuild = _marker_rebuild_required(marker, desired)

        if not rebuild and _runtime_valid(root, requirements, venv):
            confirmed = _desired_identity(root, head)
            if confirmed != desired:
                raise ToolEnvironmentError("tool bootstrap inputs changed during validation")
            if marker is not None and marker.get("source_head") == head:
                return {"status": "ready", "venv": str(venv), "marker": str(marker_path)}
            refreshed = dict(marker or {})
            refreshed.update(desired)
            refreshed["verified_at"] = now_utc()
            marker_path.write_text(json.dumps(refreshed, sort_keys=True) + "\n", encoding="utf-8")
            return {"status": "refreshed", "venv": str(venv), "marker": str(marker_path)}
    except OSError as exc:
        raise ToolEnvironmentError(str(exc)) from exc

    try:
        _create_venv(root, requirements, venv)
        if not _runtime_valid(root, requirements, venv):
            raise ToolEnvironmentError("new tools/.venv failed post-provision validation")
        confirmed = _desired_identity(root, head)
        if confirmed != desired:
            raise ToolEnvironmentError("tool bootstrap inputs changed during provisioning")
        generated = dict(desired)
        generated.update({
            "generation_id": uuid.uuid4().hex,
            "provisioned_at": now_utc(),
            "verified_at": now_utc(),
        })
        marker_path.write_text(json.dumps(generated, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ToolEnvironmentError) as exc:
        try:
            _remove_venv(venv)
        except OSError:
            pass
        if isinstance(exc, ToolEnvironmentError):
            raise
        raise ToolEnvironmentError(str(exc)) from exc
    return {"status": "provisioned", "venv": str(venv), "marker": str(marker_path)}


def preflight_tool_environment(*, task_gid: str, agent_id: str | None, worktree: Path, head: str) -> dict[str, Any]:
    try:
        return ensure_tool_environment(worktree, head)
    except ToolEnvironmentError as exc:
        retry = f"tools/agent-worktree resume --task {task_gid}"
        if agent_id:
            retry += f" --agent-id {agent_id}"
        fail(
            "PRELAUNCH_BOOTSTRAP_FAILED",
            f"worktree-local tools/.venv provisioning failed before agent/test launch: {exc}. Retry exactly: {retry}",
        )
