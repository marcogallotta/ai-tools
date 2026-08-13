from __future__ import annotations

from agent_worktree_support import Harness, assert_error, git, git_out, h, payload


def adopt(h: Harness, task: str, branch: str, expected_head: str, *, base: str | None = None, agent: str | None = None, check: bool = True):
    args = ["adopt", "--task", task, "--branch", branch, "--base-ref", "refs/heads/main", "--base", base or h.current_remote_main(), "--expected-head", expected_head, "--json"]
    if agent:
        args.extend(["--agent-id", agent])
    return h.tool(*args, check=check)


def test_adopt_existing_remote_branch_enters_normal_lifecycle(h: Harness) -> None:
    base = h.current_remote_main()
    head = h.remote_branch_commit("agent/chatgpt-handoff", "chatgpt handoff", start=base)
    h.agent_file("local-implementation")
    data = payload(adopt(h, "1217451450280422", "agent/chatgpt-handoff", head, base=base, agent="local-implementation"))
    state = h.state("1217451450280422")
    wt = h.wt("1217451450280422")
    assert data["command"] == "adopt" and data["remote_relation"] == "equal"
    assert state["local_head"] == state["published_head"] == state["remote_owned_head"] == head
    assert state["owner"]["agent_id"] == "local-implementation"
    assert git_out(wt, "rev-parse", "HEAD") == head
    assert git_out(wt, "symbolic-ref", "--short", "HEAD") == "agent/chatgpt-handoff"
    assert payload(h.tool("resume", "--task", "1217451450280422", "--agent-id", "local-implementation", "--json"))["remote_relation"] == "equal"
    assert payload(h.tool("publish", "--task", "1217451450280422", "--json"))["remote_relation"] == "equal"
    assert payload(h.tool("verify-handoff", "--task", "1217451450280422", "--json"))["remote_owned_head"] == head
    assert payload(h.tool("cleanup", "--task", "1217451450280422", "--disposition", "closed", "--json"))["disposition"] == "closed"


def test_adopt_refuses_moved_remote_without_local_mutation(h: Harness) -> None:
    base = h.current_remote_main()
    old = h.remote_branch_commit("agent/moving-handoff", "first", start=base)
    h.remote_branch_commit("agent/moving-handoff", "second")
    result = adopt(h, "2001", "agent/moving-handoff", old, base=base, check=False)
    assert_error(result, "EXPECTED_HEAD_MISMATCH")
    assert not h.state_path("2001").exists() and not h.wt("2001").exists()
    assert git(h.primary, "show-ref", "--verify", "refs/heads/agent/moving-handoff", check=False).returncode != 0
