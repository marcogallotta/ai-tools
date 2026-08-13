from __future__ import annotations

from agent_worktree_support import Harness, assert_error, git, git_out, h, payload

def test_adopt_fresh_remote_branch_creates_locked_owned_worktree(h: Harness) -> None:
    h.agent_file("claude-1")
    sha = h.remote_branch_commit("agent/adopted-fresh", "fresh", start=h.current_remote_main())
    assert git(h.primary, "show-ref", "--verify", "--quiet", "refs/heads/agent/adopted-fresh", check=False).returncode != 0

    result = h.adopt(task="2001", branch="agent/adopted-fresh", expected_head=sha)
    data = payload(result)
    state = h.state(task="2001")

    assert data["local_head"] == sha
    assert data["published_head"] == sha
    assert data["remote_relation"] == "equal"
    assert git_out(h.wt("2001"), "rev-parse", "HEAD") == sha
    assert git_out(h.wt("2001"), "symbolic-ref", "--short", "HEAD") == "agent/adopted-fresh"
    assert state["lifecycle"] == "active"
    assert state["adopted"] is True
    assert state["base_sha"] == git_out(h.primary, "merge-base", sha, h.current_remote_main())
    porcelain = git_out(h.primary, "worktree", "list", "--porcelain")
    assert str(h.wt("2001")) in porcelain and "locked" in porcelain


def test_adopt_matching_preexisting_local_branch_is_reused(h: Harness) -> None:
    h.agent_file("claude-1")
    sha = h.remote_branch_commit("agent/adopted-local", "local-preexisting", start=h.current_remote_main())
    git(h.primary, "fetch", "origin", "agent/adopted-local", env=h.env)
    git(h.primary, "branch", "agent/adopted-local", sha)

    result = h.adopt(task="2002", branch="agent/adopted-local", expected_head=sha)
    data = payload(result)
    assert data["local_head"] == sha
    assert git_out(h.wt("2002"), "rev-parse", "HEAD") == sha


def test_adopt_head_mismatch_rejected(h: Harness) -> None:
    h.agent_file("claude-1")
    sha = h.remote_branch_commit("agent/adopted-mismatch", "actual", start=h.current_remote_main())
    wrong = h.current_remote_main()

    result = h.adopt(task="2003", branch="agent/adopted-mismatch", expected_head=wrong, check=False)
    assert_error(result, "ADOPT_HEAD_MISMATCH")
    assert git(h.primary, "show-ref", "--verify", "--quiet", "refs/heads/agent/adopted-mismatch", check=False).returncode != 0
    assert not h.state_path("2003").exists()


def test_adopt_refuses_when_task_state_already_exists(h: Harness) -> None:
    h.agent_file("claude-1")
    h.start(task="2004", branch="agent/already-owned", agent="claude-1")
    sha = h.remote_branch_commit("agent/adopted-existing-task", "text", start=h.current_remote_main())

    result = h.adopt(task="2004", branch="agent/adopted-existing-task", expected_head=sha, check=False)
    assert_error(result, "STATE_CONTRADICTION")
    assert git(h.primary, "show-ref", "--verify", "--quiet", "refs/heads/agent/adopted-existing-task", check=False).returncode != 0


def test_adopt_local_branch_mismatch_rejected_without_touching_it(h: Harness) -> None:
    h.agent_file("claude-1")
    sha = h.remote_branch_commit("agent/adopted-conflict", "remote-version", start=h.current_remote_main())
    stale_local = h.current_remote_main()
    git(h.primary, "branch", "agent/adopted-conflict", stale_local)

    result = h.adopt(task="2005", branch="agent/adopted-conflict", expected_head=sha, check=False)
    assert_error(result, "ADOPT_LOCAL_BRANCH_MISMATCH")
    # The pre-existing local branch must be left exactly as it was, not deleted or moved.
    assert git_out(h.primary, "rev-parse", "refs/heads/agent/adopted-conflict") == stale_local
    assert not h.state_path("2005").exists()


def test_adopt_post_worktree_verify_failure_leaves_no_worktree_branch_or_state(h: Harness) -> None:
    # A same-named tag outranks refs/heads/<name> in Git's ref-resolution order,
    # so "git worktree add <path> <branch>" resolves the tag instead of the
    # branch and checks out under a disambiguated "heads/<branch>" name -- a
    # genuine way for the worktree to be created successfully and then fail
    # verification (BRANCH_MISMATCH), without any monkeypatching.
    h.agent_file("claude-1")
    sha = h.remote_branch_commit("agent/adopted-tag-shadow", "text", start=h.current_remote_main())
    git(h.primary, "tag", "agent/adopted-tag-shadow", h.current_remote_main())

    result = h.adopt(task="2007", branch="agent/adopted-tag-shadow", expected_head=sha, check=False)
    assert_error(result, "BRANCH_MISMATCH")
    assert git(h.primary, "show-ref", "--verify", "--quiet", "refs/heads/agent/adopted-tag-shadow", check=False).returncode != 0
    porcelain = git_out(h.primary, "worktree", "list", "--porcelain")
    assert str(h.wt("2007")) not in porcelain
    assert not h.wt("2007").exists()
    assert not h.state_path("2007").exists()


def test_adopt_failed_base_precondition_leaves_no_branch_worktree_or_state(h: Harness) -> None:
    h.agent_file("claude-1")
    sha = h.remote_branch_commit("agent/adopted-bad-base", "text", start=h.current_remote_main())

    result = h.adopt(
        task="2006",
        branch="agent/adopted-bad-base",
        expected_head=sha,
        base_ref="refs/heads/does-not-exist",
        check=False,
    )
    assert_error(result, "REMOTE_REF_MISSING")
    assert git(h.primary, "show-ref", "--verify", "--quiet", "refs/heads/agent/adopted-bad-base", check=False).returncode != 0
    assert not h.wt("2006").exists()
    assert not h.state_path("2006").exists()
