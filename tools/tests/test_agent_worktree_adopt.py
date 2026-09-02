from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from agent_worktree_support import Harness, SCRIPT, TOOLS_DIR, assert_error, git, git_out, h, payload

sys.path.insert(0, str(TOOLS_DIR))
from agent_worktree_lib.common import GitRunner  # noqa: E402
from agent_worktree_lib.start_resume import _rollback_local_adoption  # noqa: E402


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


def test_concurrent_adopt_race_never_deletes_unrelated_branch(h: Harness) -> None:
    base = h.current_remote_main()
    head = h.remote_branch_commit("agent/adopt-race", "handoff", start=base)
    h.github_reviews.write_text(
        json.dumps(
            {
                "42": {
                    "pr": {
                        "state": "open",
                        "body": "Implements Asana tasks 3001 and 3002.",
                        "head": {"ref": "agent/adopt-race", "sha": head},
                    },
                    "reviews": {},
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def args(task: str, agent: str) -> list[str]:
        h.agent_file(agent, owning_task_gid=task)
        adopt_argv = [
            "python3", str(SCRIPT), "adopt", "--task", task, "--branch", "agent/adopt-race",
            "--base-ref", "refs/heads/main", "--base", base, "--expected-head", head,
            "--agent-id", agent, "--json",
        ]
        return [
            "python3", str(SCRIPT), "claim", "--task", task, "--branch", "agent/adopt-race",
            "--agent-id", agent, "--pr-number", "42", "--pr-head", head, "--pr-lease-state", "none",
            "--", *adopt_argv,
        ]

    p1 = subprocess.Popen(args("3001", "adopt-race-a"), cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(args("3002", "adopt-race-b"), cwd=h.primary, env=h.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    o1, e1 = p1.communicate(timeout=30)
    o2, e2 = p2.communicate(timeout=30)

    results = [(p1.returncode, o1, e1, "3001"), (p2.returncode, o2, e2, "3002")]
    winners = [r for r in results if r[0] == 0]
    losers = [r for r in results if r[0] != 0]
    assert len(winners) == 1 and len(losers) == 1, (o1, e1, o2, e2)
    winner_task = winners[0][3]
    loser_task = losers[0][3]

    # The loser must fail without ever mutating state, and in particular must never delete
    # the branch/worktree the winner provably created — even though both attempts targeted
    # the identical branch name and expected head.
    assert "ERROR" in losers[0][2]
    assert not h.state_path(loser_task).exists()
    assert not h.wt(loser_task).exists()

    winner_state = h.state(winner_task)
    assert winner_state["local_head"] == winner_state["published_head"] == winner_state["remote_owned_head"] == head
    assert git_out(h.primary, "rev-parse", "refs/heads/agent/adopt-race") == head
    assert git_out(h.wt(winner_task), "rev-parse", "HEAD") == head
    records = git_out(h.primary, "worktree", "list", "--porcelain").split("\n\n")
    assert sum(str(h.wt(winner_task)) in record for record in records) == 1
    assert payload(h.tool("verify-handoff", "--task", winner_task, "--json"))["remote_owned_head"] == head


def test_rollback_never_deletes_branch_it_does_not_own(h: Harness) -> None:
    base = h.current_remote_main()
    head = h.remote_branch_commit("agent/owner-race", "handoff", start=base)
    git(h.primary, "fetch", "origin", "agent/owner-race", env=h.env)

    # Simulate a concurrent winner: a branch at exactly the expected head, created
    # by someone else, never checked out anywhere -- indistinguishable by SHA or
    # checkout state from a branch this attempt itself just created.
    git(h.primary, "branch", "agent/owner-race", head)

    runner = GitRunner()
    repo = SimpleNamespace(source_top=h.primary)
    candidate = h.wt("unregistered-task")

    unowned_error = _rollback_local_adoption(
        runner, repo=repo, candidate=candidate, branch="agent/owner-race",
        expected_head=head, owns_branch=False,
    )
    assert unowned_error is None
    assert git_out(h.primary, "rev-parse", "refs/heads/agent/owner-race") == head

    owned_error = _rollback_local_adoption(
        runner, repo=repo, candidate=candidate, branch="agent/owner-race",
        expected_head=head, owns_branch=True,
    )
    assert owned_error is None
    assert git(h.primary, "show-ref", "--verify", "refs/heads/agent/owner-race", check=False).returncode != 0


def test_adopt_refuses_moved_remote_without_local_mutation(h: Harness) -> None:
    base = h.current_remote_main()
    old = h.remote_branch_commit("agent/moving-handoff", "first", start=base)
    h.remote_branch_commit("agent/moving-handoff", "second")
    result = adopt(h, "2001", "agent/moving-handoff", old, base=base, check=False)
    assert_error(result, "PR_BRANCH_HEAD_MISMATCH")
    assert not h.state_path("2001").exists() and not h.wt("2001").exists()
    assert git(h.primary, "show-ref", "--verify", "refs/heads/agent/moving-handoff", check=False).returncode != 0
