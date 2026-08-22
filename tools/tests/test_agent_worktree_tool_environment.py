from __future__ import annotations

import json
import shutil
import sys

from agent_worktree_support import TOOLS_DIR, Harness, git_out, h, payload

sys.path.insert(0, str(TOOLS_DIR))
from agent_worktree_lib import tool_environment as tool_env  # noqa: E402


def _marker(h: Harness, task: str) -> dict[str, object]:
    path = h.wt(task) / "tools/.venv" / tool_env.MARKER_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_start_provisions_worktree_local_tools_runtime_without_source_mutation(h: Harness) -> None:
    task = "5101"
    h.start(task=task, branch="agent/tool-env-start")
    worktree = h.wt(task)
    venv = worktree / "tools/.venv"
    marker = _marker(h, task)

    assert (venv / "bin/python").exists()
    assert marker["worktree"] == str(worktree.resolve())
    assert marker["source_head"] == h.state(task)["local_head"]
    assert git_out(worktree, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not (h.primary / "tools/.venv").exists()


def test_fresh_adopt_provisions_same_runtime_before_return(h: Harness) -> None:
    task = "5102"
    branch = "agent/tool-env-adopt"
    base = h.current_remote_main()
    head = h.remote_branch_commit(branch, "handoff", start=base)
    h.tool(
        "adopt", "--task", task, "--branch", branch,
        "--base-ref", "refs/heads/main", "--base", base,
        "--expected-head", head, "--json",
    )

    marker = _marker(h, task)
    assert marker["worktree"] == str(h.wt(task).resolve())
    assert marker["source_head"] == head
    assert (h.wt(task) / "tools/.venv/bin/python").exists()


def test_valid_runtime_is_reused_without_destructive_rebuild(h: Harness) -> None:
    task = "5103"
    h.start(task=task, branch="agent/tool-env-reuse")
    venv = h.wt(task) / "tools/.venv"
    sentinel = venv / "preserve-me"
    sentinel.write_text("valid runtime\n", encoding="utf-8")
    generation = _marker(h, task)["generation_id"]

    h.tool("resume", "--task", task, "--json")
    assert sentinel.read_text(encoding="utf-8") == "valid runtime\n"
    assert _marker(h, task)["generation_id"] == generation

    advanced_head = h.commit_local(task, "head-only change")
    h.tool("resume", "--task", task, "--json")

    refreshed = _marker(h, task)
    assert sentinel.read_text(encoding="utf-8") == "valid runtime\n"
    assert refreshed["generation_id"] == generation
    assert refreshed["source_head"] == advanced_head


def test_requirements_identity_change_rebuilds_stale_runtime(h: Harness) -> None:
    task = "5104"
    h.start(task=task, branch="agent/tool-env-requirements")
    venv = h.wt(task) / "tools/.venv"
    sentinel = venv / "stale-runtime"
    sentinel.write_text("old\n", encoding="utf-8")
    generation = _marker(h, task)["generation_id"]

    (h.wt(task) / "tools/requirements.txt").write_text("# dependency input changed\n", encoding="utf-8")
    h.tool("resume", "--task", task, "--json")

    assert not sentinel.exists()
    assert _marker(h, task)["generation_id"] != generation


def test_interpreter_identity_change_rebuilds_the_real_runtime(h: Harness) -> None:
    task = "5110"
    h.start(task=task, branch="agent/tool-env-interpreter")
    venv = h.wt(task) / "tools/.venv"
    marker_path = venv / tool_env.MARKER_NAME
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    generation = marker["generation_id"]
    marker["bootstrap_interpreter"] = {
        "executable": "/stale/python",
        "implementation": "CPython",
        "version": "0.0.0",
    }
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    sentinel = venv / "stale-interpreter-runtime"
    sentinel.write_text("old\n", encoding="utf-8")

    h.tool("resume", "--task", task, "--json")

    refreshed = _marker(h, task)
    assert not sentinel.exists()
    assert refreshed["generation_id"] != generation
    assert refreshed["bootstrap_interpreter"] != marker["bootstrap_interpreter"]


def test_exec_fails_typed_before_launch_when_bootstrap_inputs_are_missing(h: Harness) -> None:
    task = "5105"
    h.start(task=task, branch="agent/tool-env-failure")
    shutil.rmtree(h.wt(task) / "tools/.venv")
    (h.wt(task) / "tools/requirements.txt").unlink()
    launched = h.wt(task) / "agent-launched"

    result = h.tool(
        "exec", "--task", task, "--", "sh", "-c", f"touch {launched}",
        check=False,
    )

    assert result.returncode != 0
    assert "ERROR PRELAUNCH_BOOTSTRAP_FAILED:" in result.stderr
    assert not launched.exists()


def test_each_worktree_owns_its_runtime_and_resume_repairs_only_that_worktree(h: Harness) -> None:
    h.start(task="5106", branch="agent/tool-env-a")
    h.start(task="5107", branch="agent/tool-env-b")
    a_venv = h.wt("5106") / "tools/.venv"
    b_venv = h.wt("5107") / "tools/.venv"
    b_sentinel = b_venv / "keep-b"
    b_sentinel.write_text("b\n", encoding="utf-8")

    shutil.rmtree(a_venv)
    h.tool("resume", "--task", "5106", "--json")

    assert (a_venv / "bin/python").exists()
    assert b_sentinel.read_text(encoding="utf-8") == "b\n"
    assert _marker(h, "5106")["worktree"] != _marker(h, "5107")["worktree"]


def test_resume_recreates_missing_runtime_after_recovery_boundary(h: Harness) -> None:
    task = "5108"
    h.start(task=task, branch="agent/tool-env-recover")
    shutil.rmtree(h.wt(task) / "tools/.venv")

    h.tool("resume", "--task", task, "--json")

    assert (h.wt(task) / "tools/.venv/bin/python").exists()
    assert _marker(h, task)["source_head"] == h.state(task)["local_head"]


def test_cleanup_treats_only_canonical_tools_runtime_as_disposable_generated_state(h: Harness) -> None:
    task = "5109"
    branch = "agent/tool-env-cleanup"
    h.start(task=task, branch=branch)
    head = str(h.state(task)["local_head"])
    h.tool("publish", "--task", task, "--json")

    data = payload(h.tool(
        "cleanup", "--task", task, "--branch", branch, "--expected-head", head,
        "--pr-number", "42", "--disposition", "closed", "--json",
    ))

    assert data["worktree_removed"] is True
    assert not h.wt(task).exists()
