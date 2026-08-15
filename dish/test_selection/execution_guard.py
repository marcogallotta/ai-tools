"""Fail closed when governed tests run from the protected primary checkout."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


class TestExecutionRefused(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise TestExecutionRefused("cannot verify repository/worktree identity; execution refused")
    return completed.stdout.strip()


def require_safe_test_checkout(root: Path, *, expected_head: str | None = None) -> str:
    root = root.resolve()
    git_dir_raw = _git(root, "rev-parse", "--git-dir")
    common_dir_raw = _git(root, "rev-parse", "--git-common-dir")
    git_dir = (
        (root / git_dir_raw).resolve()
        if not Path(git_dir_raw).is_absolute()
        else Path(git_dir_raw).resolve()
    )
    common_dir = (
        (root / common_dir_raw).resolve()
        if not Path(common_dir_raw).is_absolute()
        else Path(common_dir_raw).resolve()
    )
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "HEAD")

    if git_dir == common_dir or branch == "main":
        raise TestExecutionRefused(
            "protected primary/main checkout; use the task-owned worktree at the exact candidate HEAD"
        )
    if expected_head is not None:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise TestExecutionRefused("expected candidate HEAD must be a full lowercase Git SHA")
        if head != expected_head:
            raise TestExecutionRefused(
                f"candidate HEAD mismatch: expected {expected_head}, found {head}"
            )
    return head
