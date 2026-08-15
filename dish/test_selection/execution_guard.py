"""Fail closed when governed tests run from the protected primary checkout."""
from __future__ import annotations

import os
import pwd
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


def _protected_primary_root() -> Path:
    try:
        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError) as exc:
        raise TestExecutionRefused(
            "cannot resolve the host account's protected primary checkout; execution refused"
        ) from exc
    return (Path(account_home) / "ai-tools").resolve()


def require_safe_test_checkout(root: Path, *, expected_head: str | None = None) -> str:
    root = root.resolve()
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "HEAD")
    protected_primary = _protected_primary_root()

    if top == protected_primary or branch == "main":
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
