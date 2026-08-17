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


def test_commit_merge_records_two_parent_reconciliation_commit(h: Harness) -> None:
    wt = _prepare(h)
    target = h.advance_main()
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    result = h.tool(
        "commit", "--task", "1001", "-m", "reconcile", "--merge-target-head", target,
        "--json", "--", "tracked.txt",
    )
    data = payload(result)
    assert data["merge_target_head"] == target
    new_head = data["new_head"]
    parents = git_out(wt, "log", "-1", "--format=%P", new_head).split()
    assert parents == [h.base, target]


def test_commit_merge_rejects_stale_target(h: Harness) -> None:
    wt = _prepare(h)
    stale = h.advance_main()
    h.advance_main()
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    refused = h.tool(
        "commit", "--task", "1001", "-m", "reconcile", "--merge-target-head", stale,
        "--", "tracked.txt", check=False,
    )
    assert_error(refused, "STALE_MERGE_TARGET")
    assert git_out(wt, "rev-parse", "HEAD") == h.base


def test_commit_merge_rejects_target_that_moves_during_commit_preparation(h: Harness) -> None:
    wt = _prepare(h)
    target = h.advance_main()
    moved = h.remote_branch_commit("race-target", "move during reconciliation", start=target)
    counter = h.root / "race-ssh-count"
    h.ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shlex, subprocess, sys\n"
        "from pathlib import Path\n"
        "counter = Path(os.environ['TEST_SSH_COUNTER'])\n"
        "count = int(counter.read_text() or '0') + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "cmd = sys.argv[-1]\n"
        "parts = shlex.split(cmd)\n"
        "if not parts or parts[0] not in ('git-upload-pack', 'git-receive-pack'):\n"
        "    raise SystemExit(f'unexpected ssh command: {cmd}')\n"
        "if count == 2:\n"
        "    subprocess.run([\n"
        "        'git', '--git-dir=' + os.environ['TEST_BARE_ORIGIN'], 'update-ref',\n"
        "        'refs/heads/main', os.environ['TEST_MOVE_MAIN_TO'],\n"
        "    ], check=True)\n"
        "os.execvp(parts[0], [parts[0], os.environ['TEST_BARE_ORIGIN']])\n",
        encoding="utf-8",
    )
    h.ssh.chmod(0o755)

    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    refused = h.tool(
        "commit", "--task", "1001", "-m", "reconcile", "--merge-target-head", target,
        "--", "tracked.txt", check=False,
        env={"TEST_SSH_COUNTER": str(counter), "TEST_MOVE_MAIN_TO": moved},
    )

    assert_error(refused, "STALE_MERGE_TARGET")
    assert h.current_remote_main() == moved
    assert git_out(wt, "rev-parse", "HEAD") == h.base


def test_commit_merge_rejects_conflict_left_outside_explicit_paths(h: Harness) -> None:
    wt = _prepare(h)
    target = h.advance_main()
    (wt / "conflict.txt").write_text("orig\n", encoding="utf-8")
    git(wt, "add", "conflict.txt")
    git(wt, "commit", "-m", "add conflict file")
    base_blob = git_out(wt, "hash-object", "-w", "--stdin", input="orig\n")
    ours_blob = git_out(wt, "hash-object", "-w", "--stdin", input="ours\n")
    theirs_blob = git_out(wt, "hash-object", "-w", "--stdin", input="theirs\n")
    index_info = (
        f"100644 {base_blob} 1\tconflict.txt\n"
        f"100644 {ours_blob} 2\tconflict.txt\n"
        f"100644 {theirs_blob} 3\tconflict.txt\n"
    )
    git(wt, "update-index", "--index-info", input=index_info)

    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    refused = h.tool(
        "commit", "--task", "1001", "-m", "reconcile", "--merge-target-head", target,
        "--", "tracked.txt", check=False,
    )
    assert_error(refused, "STAGED_PATH_OUTSIDE_EXPLICIT_SET")


def test_commit_without_merge_flag_remains_single_parent(h: Harness) -> None:
    wt = _prepare(h)
    h.advance_main()
    (wt / "tracked.txt").write_text("changed\n", encoding="utf-8")
    result = h.tool("commit", "--task", "1001", "-m", "ordinary", "--json", "--", "tracked.txt")
    data = payload(result)
    parents = git_out(wt, "log", "-1", "--format=%P", data["new_head"]).split()
    assert parents == [h.base]


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
