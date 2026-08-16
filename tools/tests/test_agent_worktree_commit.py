from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_worktree_support import Harness, assert_error, git, git_out, h, payload


def _prepare(h: Harness, task: str = "1001") -> Path:
    h.start(task=task, branch=f"agent/commit-{task}")
    wt = h.wt(task)
    h._identity(wt)
    return wt


def test_commit_stages_only_explicit_paths_and_never_pushes(h: Harness) -> None:
    wt = _prepare(h)
    before = git_out(wt, "rev-parse", "HEAD")
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (wt / "other.txt").write_text("unpublished\n", encoding="utf-8")

    result = h.tool("commit", "--task", "1001", "-m", "bounded", "--json", "--", "tracked.txt")
    data = payload(result)
    assert data["previous_head"] == before
    assert data["new_head"] == git_out(wt, "rev-parse", "HEAD")
    assert data["committed_paths"] == ["tracked.txt"]
    assert git_out(wt, "show", "--format=", "--name-only", "HEAD").strip() == "tracked.txt"
    assert (wt / "other.txt").read_text(encoding="utf-8") == "unpublished\n"
    remote = git(h.origin, "show-ref", "--verify", "--hash", "refs/heads/agent/commit-1001", check=False)
    assert remote.returncode != 0


def test_commit_accepts_explicit_directory_but_rejects_pre_staged_outside_path(h: Harness) -> None:
    wt = _prepare(h)
    (wt / "dir").mkdir()
    (wt / "dir/a.txt").write_text("a\n", encoding="utf-8")
    (wt / "dir/b.txt").write_text("b\n", encoding="utf-8")
    (wt / "outside.txt").write_text("outside\n", encoding="utf-8")
    git(wt, "add", "outside.txt")

    refused = h.tool("commit", "--task", "1001", "-m", "bounded", "--", "dir", check=False)
    assert_error(refused, "STAGED_PATH_OUTSIDE_EXPLICIT_SET")
    assert git_out(wt, "rev-parse", "HEAD") == h.base

    git(wt, "reset", "outside.txt")
    result = h.tool("commit", "--task", "1001", "-m", "directory", "--json", "--", "dir")
    assert payload(result)["committed_paths"] == ["dir/a.txt", "dir/b.txt"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".", "CARPET_STAGING_REFUSED"),
        ("-A", "CARPET_STAGING_REFUSED"),
        ("-u", "CARPET_STAGING_REFUSED"),
        ("--all", "CARPET_STAGING_REFUSED"),
        ("../escape", "COMMIT_PATH_ESCAPE"),
    ],
)
def test_commit_rejects_carpet_and_path_escape(h: Harness, raw: str, expected: str) -> None:
    wt = _prepare(h)
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    result = h.tool("commit", "--task", "1001", "-m", "bad", "--", raw, check=False)
    assert_error(result, expected)


def test_commit_rejects_detached_owned_worktree(h: Harness) -> None:
    wt = _prepare(h)
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    git(wt, "checkout", "--detach")
    result = h.tool("commit", "--task", "1001", "-m", "bad", "--", "tracked.txt", check=False)
    assert_error(result, "DETACHED_HEAD")


def test_commit_rejects_tampered_worktree_identity(h: Harness) -> None:
    wt = _prepare(h)
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    state_path = h.state_path()
    state = h.state()
    state["git_dir"] = str(h.primary / ".git")
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    result = h.tool("commit", "--task", "1001", "-m", "bad", "--", "tracked.txt", check=False)
    assert result.returncode != 0
    assert "ERROR GIT_DIR_MISMATCH:" in result.stderr or "ERROR STATE_INVALID:" in result.stderr


def test_commit_requires_live_claim(h: Harness) -> None:
    wt = _prepare(h)
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    result = h.raw_tool("commit", "--task", "1001", "-m", "bad", "--", "tracked.txt", check=False)
    assert_error(result, "OWNERSHIP_CLAIM_REQUIRED")


def test_commit_preserves_dish_version_guard(h: Harness) -> None:
    (h.seed / "DISH_VERSION").write_text(
        "PROTOCOL_VERSION=1\nSCHEMA_VERSION=1\n", encoding="utf-8"
    )
    (h.seed / "dish-task-schema.json").write_text(
        '{"protocol_version":"1","type":"object"}\n', encoding="utf-8"
    )
    git(h.seed, "add", "DISH_VERSION", "dish-task-schema.json")
    git(h.seed, "commit", "-m", "schema baseline")
    git(h.seed, "push", "origin", "main")
    git(h.primary, "fetch", "origin", "main", env=h.env)
    git(h.primary, "reset", "--hard", "origin/main")

    wt = _prepare(h)
    (wt / "dish-task-schema.json").write_text(
        '{"protocol_version":"1","type":"object","properties":{"x":{"type":"string"}}}\n',
        encoding="utf-8",
    )
    refused = h.tool(
        "commit", "--task", "1001", "-m", "schema", "--", "dish-task-schema.json", check=False
    )
    assert_error(refused, "COMMIT_GUARD_REFUSED")

    (wt / "DISH_VERSION").write_text(
        "PROTOCOL_VERSION=2\nSCHEMA_VERSION=2\n", encoding="utf-8"
    )
    result = h.tool(
        "commit", "--task", "1001", "-m", "schema with version", "--json", "--",
        "dish-task-schema.json", "DISH_VERSION",
    )
    assert set(payload(result)["committed_paths"]) == {"DISH_VERSION", "dish-task-schema.json"}
