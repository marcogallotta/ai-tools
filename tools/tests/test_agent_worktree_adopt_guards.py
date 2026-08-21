from __future__ import annotations

from agent_worktree_support import Harness, assert_error, git, h, payload


def adopt(h: Harness, task: str, branch: str, expected_head: str, *, base: str | None = None, check: bool = True, env: dict[str, str] | None = None):
    return h.tool(
        "adopt", "--task", task, "--branch", branch,
        "--base-ref", "refs/heads/main",
        "--base", base or h.current_remote_main(),
        "--expected-head", expected_head, "--json",
        check=check, env=env,
    )


def test_adopt_admits_second_branch_as_parallel_lineage_and_refuses_checked_out_branch(h: Harness) -> None:
    # Adopting a second, distinct branch for a task that already has an active
    # lineage is no longer ambiguous: same-task/different-branch is exactly the
    # supported parallel-lineage case (see
    # test_same_task_different_branches_are_distinct_concurrent_lineages).
    h.start(task="2002", branch="agent/already-owned")
    base = h.current_remote_main()
    head = h.remote_branch_commit("agent/second-lineage", "handoff", start=base)
    data = payload(adopt(h, "2002", "agent/second-lineage", head, base=base))
    assert data["branch"] == "agent/second-lineage"
    first = h.state("2002", "agent/already-owned")
    second = h.state("2002", "agent/second-lineage")
    assert first["lineage_id"] != second["lineage_id"]

    base = h.current_remote_main()
    head = h.remote_branch_commit("agent/checked-out", "handoff", start=base)
    git(h.primary, "fetch", "origin", "refs/heads/agent/checked-out:refs/heads/agent/checked-out", env=h.env)
    git(h.primary, "switch", "agent/checked-out")
    result = adopt(h, "2003", "agent/checked-out", head, base=base, check=False)
    assert_error(result, "BRANCH_CHECKED_OUT")
    assert not h.state_path("2003").exists() and not h.wt("2003").exists()


def test_adopt_refuses_invalid_branch_unsafe_path_and_wrong_repository(h: Harness) -> None:
    assert_error(adopt(h, "2004", "main", h.current_remote_main(), check=False), "INVALID_BRANCH")

    base = h.current_remote_main()
    head = h.remote_branch_commit("agent/unsafe", "handoff", start=base)
    result = adopt(h, "2005", "agent/unsafe", head, base=base, check=False, env={"DISH_WORKTREE_ROOT": str(h.primary / "nested-worktrees")})
    assert_error(result, "WORKTREE_PATH")
    assert not h.state_path("2005").exists()

    head = h.remote_branch_commit("agent/wrong-repo", "handoff", start=base)
    git(h.primary, "remote", "set-url", "origin", str(h.origin))
    assert_error(adopt(h, "2006", "agent/wrong-repo", head, base=base, check=False), "ORIGIN_IDENTITY")
