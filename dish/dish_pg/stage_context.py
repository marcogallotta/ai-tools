"""Current Honest-main stage context returned with a successful PostgreSQL start.

Stage protocol text is never pinned to a task or contract binding. Every stage uses
the newest Honest Pantry `main` protocol, including already-open Verification
cycles. The checkout is refreshed with the same ff-only origin/main rule used by
`dish_honest_read`. Context assembly is response enrichment only: a refresh/read
failure returns `available: false` and never rewrites workflow state.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

STAGE_CONTEXT_FORMAT = "dish-stage-context-v2"
STAGE_CONTEXT_MAX_TEXT_BYTES = 160 * 1024
STAGE_CONTEXT_GUIDANCE = (
    "The current stage protocol from Honest Pantry main is included here; do not re-read it."
)
REPOSITORY_INSTRUCTIONS_PATH = "CLAUDE.md"
STAGE_PROTOCOL_PATHS = {
    "planning": "dish-planning-protocol.md",
    "research": "dish-research-protocol.md",
    "verification": "dish-verification-protocol.md",
}
_START_KIND_ROLES = {
    "planning": "planning",
    "initial": "research",
    "change": "research",
    "verification": "verification",
}
HONEST_GIT_SSH_COMMAND = "/usr/bin/ssh -F /dev/null"
_GIT_TIMEOUT_SECONDS = 10
_HONEST_LOCK = threading.Lock()
LOG = logging.getLogger("dish.stage_context")


class StageContextUnavailable(RuntimeError):
    """Current Honest main cannot be served exactly."""


def role_for_start_kind(kind: object) -> str | None:
    return _START_KIND_ROLES.get(str(kind or ""))


def _git(root: Path, *args: str, timeout: int = _GIT_TIMEOUT_SECONDS) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/git", *args],
            cwd=root,
            capture_output=True,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": HONEST_GIT_SSH_COMMAND,
            },
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StageContextUnavailable(
            f"Honest Pantry main refresh failed ({exc})"
        ) from exc
    if result.returncode:
        stderr_lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = stderr_lines[-1] if stderr_lines else (
            f"git {' '.join(args)} exited {result.returncode}"
        )
        raise StageContextUnavailable(
            f"Honest Pantry main refresh failed ({reason})"
        )
    return result.stdout


def _current_main_commit(root: Path) -> str:
    root = Path(root).expanduser().resolve()
    with _HONEST_LOCK:
        branch = _git(root, "branch", "--show-current").decode().strip()
        if branch != "main":
            raise StageContextUnavailable(
                "Honest Pantry checkout is not on main"
            )
        try:
            _git(root, "pull", "--ff-only", "origin", "main", timeout=30)
        except StageContextUnavailable:
            _git(root, "pull", "--ff-only", "origin", "main", timeout=30)
        sha = _git(root, "rev-parse", "HEAD").decode().strip()
        fetched = _git(root, "rev-parse", "FETCH_HEAD").decode().strip()
        if sha != fetched:
            raise StageContextUnavailable(
                "Honest Pantry checkout does not match origin/main"
            )
        return sha


def _file_entry(root: Path, commit: str, path: str, *, kind: str) -> dict[str, Any]:
    try:
        content = _git(root, "show", f"{commit}:{path}")
    except StageContextUnavailable as exc:
        raise StageContextUnavailable(
            f"{path} is unavailable at current Honest main {commit}"
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageContextUnavailable(f"{path} is not UTF-8 text") from exc
    return {
        "kind": kind,
        "path": path,
        "git_commit": commit,
        "sha256": digest,
        "bytes": len(content),
        "text": text,
    }


def build_stage_context(
    *,
    role: str,
    honest_root: Path,
    max_text_bytes: int = STAGE_CONTEXT_MAX_TEXT_BYTES,
) -> dict[str, Any]:
    path = STAGE_PROTOCOL_PATHS.get(role)
    if path is None:
        raise StageContextUnavailable(f"no stage protocol is mapped for role {role!r}")
    root = Path(honest_root).expanduser().resolve()
    commit = _current_main_commit(root)
    protocol = _file_entry(root, commit, path, kind="stage_protocol")
    if protocol["bytes"] > max_text_bytes:
        raise StageContextUnavailable(
            f"{path} exceeds the {max_text_bytes}-byte stage context budget"
        )
    served = [protocol]
    omitted: list[dict[str, str]] = []
    try:
        instructions = _file_entry(
            root, commit, REPOSITORY_INSTRUCTIONS_PATH,
            kind="repository_instructions",
        )
    except StageContextUnavailable as exc:
        omitted.append({"path": REPOSITORY_INSTRUCTIONS_PATH, "reason": str(exc)})
    else:
        if protocol["bytes"] + instructions["bytes"] > max_text_bytes:
            omitted.append({
                "path": REPOSITORY_INSTRUCTIONS_PATH,
                "reason": f"exceeds the {max_text_bytes}-byte stage context budget",
            })
        else:
            served.append(instructions)
    return {
        "available": True,
        "format": STAGE_CONTEXT_FORMAT,
        "role": role,
        "source": {
            "repository": "marcogallotta/honest-pantry",
            "ref": "refs/heads/main",
            "honest_commit": commit,
        },
        "files": served,
        "omitted": omitted,
    }


def stage_context_for_start(
    *,
    kind: object,
    honest_root: Path,
) -> dict[str, Any] | None:
    role = role_for_start_kind(kind)
    if role is None:
        return None
    try:
        return build_stage_context(role=role, honest_root=honest_root)
    except StageContextUnavailable as exc:
        reason = str(exc)
    except Exception as exc:  # enrichment must never fail a committed start
        LOG.exception("stage_context_failure role=%s", role)
        reason = f"stage context assembly failed ({type(exc).__name__})"
    return {
        "available": False,
        "format": STAGE_CONTEXT_FORMAT,
        "role": role,
        "reason": reason,
    }
