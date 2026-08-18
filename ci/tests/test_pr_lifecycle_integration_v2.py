from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_integration_certification import LocalIntegrationCertificationMixin
from pr_lifecycle_local_integration import (
    ATTEMPT_RESULT_SCHEMA,
    MAX_INTEGRATION_ATTEMPTS,
    LocalIntegrationFence,
    LocalIntegrationLauncher,
    load_attempt_result,
)
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_support import LifecycleError, LifecycleState, STATE_LABELS


HEAD = "1" * 40
TARGET_SHA = "2" * 40
MERGE_SHA = "3" * 40


def _fence(tmp_path: Path) -> LocalIntegrationFence:
    return LocalIntegrationFence(
        repository="marcogallotta/ai-tools",
        pr_number=123,
        branch="agent/example",
        head=HEAD,
        review_id=77,
        task_ids=["1217519197662916"],
        main_sha=TARGET_SHA,
        target_branch="main",
        handoff_comment_id=88,
        handoff_key_value="abc123",
        root=tmp_path,
    )


def _context() -> dict:
    return {
        "schema": "dish-pr-local-integration-v1",
        "repository": "marcogallotta/ai-tools",
        "task_ids": ["1217519197662916"],
        "pull_request": {
            "number": 123,
            "url": "https://github.com/marcogallotta/ai-tools/pull/123",
            "branch": "agent/example",
            "head": HEAD,
            "base": "feature-base",
            "body": "",
        },
        "target": {
            "branch": "main",
            "ref": "refs/heads/main",
            "observed_sha": TARGET_SHA,
        },
    }


def _wait_result(path: Path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.02)
    raise AssertionError(f"attempt result was not written: {path}")


def test_exit_zero_retains_complete_attempt_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    fence = _fence(tmp_path)
    assert fence.acquire()
    launcher = LocalIntegrationLauncher(
        f"{sys.executable} -c \"import sys; print('stdout-zero'); print('stderr-zero', file=sys.stderr)\""
    )
    attempt = launcher.dispatch_background(_context(), fence=fence)
    result = _wait_result(Path(attempt["result_path"]))

    assert result["schema"] == ATTEMPT_RESULT_SCHEMA
    assert result["process_exit_code"] == 0
    assert result["outcome"] == "PROCESS_EXIT_ZERO"
    assert "stdout-zero" in Path(result["stdout_path"]).read_text(encoding="utf-8")
    assert "stderr-zero" in Path(result["stderr_path"]).read_text(encoding="utf-8")
    state = fence.recovery_state()
    assert state is not None
    assert load_attempt_result(state)["attempt_id"] == result["attempt_id"]


def test_long_child_is_background_and_running_requires_real_flock_and_process(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    fence = _fence(tmp_path)
    assert fence.acquire()
    launcher = LocalIntegrationLauncher(f"{sys.executable} -c \"import time; time.sleep(1.0)\"")

    started = time.monotonic()
    attempt = launcher.dispatch_background(_context(), fence=fence)
    elapsed = time.monotonic() - started
    assert elapsed < 0.6

    deadline = time.monotonic() + 2.0
    live = fence.liveness()
    while time.monotonic() < deadline and not live["running"]:
        time.sleep(0.02)
        live = fence.liveness()
    assert live["running"] is True
    assert live["lock_held"] is True
    assert live["process_alive"] is True

    _wait_result(Path(attempt["result_path"]))


def test_stale_active_claim_with_released_flock_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    fence = _fence(tmp_path)
    assert fence.acquire()
    # Leave the persisted claim active, but release the real OS admission witness.
    fence.release()
    state = fence.recovery_state()
    assert state is not None and state["status"] == "active"

    live = fence.liveness()
    assert live["lock_held"] is False
    assert live["running"] is False


def test_transient_503_is_typed_retryable_and_budget_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("DISH_LOCAL_INTEGRATION_STATE_ROOT", str(tmp_path))
    fence = _fence(tmp_path)
    assert fence.acquire()
    launcher = LocalIntegrationLauncher(
        f"{sys.executable} -c \"import sys; print('HTTP 503 Service Unavailable', file=sys.stderr); raise SystemExit(1)\""
    )
    attempt = launcher.dispatch_background(_context(), fence=fence)
    result = _wait_result(Path(attempt["result_path"]))
    assert result["process_exit_code"] == 1
    assert result["outcome"] == "PROCESS_FAILED"
    assert result["retryable"] is True

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and fence.liveness()["lock_held"]:
        time.sleep(0.02)
    assert fence.liveness()["lock_held"] is False

    # Each replacement claim consumes one bounded attempt generation.
    for _ in range(MAX_INTEGRATION_ATTEMPTS - 1):
        replacement = _fence(tmp_path)
        assert replacement.acquire()
        replacement.release()
    exhausted = _fence(tmp_path)
    assert exhausted.acquire()
    with pytest.raises(LifecycleError, match="attempt budget exhausted"):
        launcher.dispatch_background(_context(), fence=exhausted)
    exhausted.release()


class _FakeHTTP:
    def __init__(self, status: str):
        self.status = status

    def request(self, method, url, *, headers=None, body=None):
        return 200, {}, {"status": self.status}


class _FakeGitHub:
    repository = "marcogallotta/ai-tools"
    headers = {}

    def __init__(self, compare_status: str):
        self.http = _FakeHTTP(compare_status)

    def get_ref_sha(self, ref: str) -> str:
        assert ref == "heads/main"
        return TARGET_SHA

    def _url(self, path: str, query=None) -> str:
        return f"https://api.github.test/repos/{self.repository}/{path}"


class _ProofHarness(LocalIntegrationCertificationMixin):
    def __init__(self, compare_status: str):
        self.github = _FakeGitHub(compare_status)


@pytest.mark.parametrize(("compare_status", "landed"), [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)])
def test_target_specific_landing_uses_intended_default_branch(compare_status, landed):
    harness = _ProofHarness(compare_status)
    raw = {
        "merged": True,
        "merge_commit_sha": MERGE_SHA,
        "base": {
            "ref": "agent/intermediate",
            "repo": {"default_branch": "main"},
        },
    }
    proof = harness._target_landing_proof(raw)
    assert proof["target_branch"] == "main"
    assert proof["immediate_base"] == "agent/intermediate"
    assert proof["landed"] is landed


class _InspectBase:
    def inspect(self, pr):
        return SimpleNamespace(
            state=LifecycleState.MERGED,
            state_label=STATE_LABELS[LifecycleState.MERGED],
            residual_reason=None,
            human_action=None,
        )


class _InspectHarness(LocalIntegrationCertificationMixin, _InspectBase):
    def __init__(self, compare_status: str):
        self.github = _FakeGitHub(compare_status)


def test_intermediate_branch_merged_true_is_not_terminal_source_landing():
    harness = _InspectHarness("behind")
    lifecycle = harness.inspect(
        {
            "merged": True,
            "merge_commit_sha": MERGE_SHA,
            "base": {
                "ref": "agent/intermediate",
                "repo": {"default_branch": "main"},
            },
        }
    )
    assert lifecycle.state == LifecycleState.REVIEW_PASSED
    assert "target-specific source landing incomplete" in lifecycle.residual_reason
    assert lifecycle.human_action is None


def test_operator_readiness_separates_source_from_activation():
    pr = SimpleNamespace(
        state=LifecycleState.MERGED,
        head=HEAD,
        number=123,
        state_label=STATE_LABELS[LifecycleState.MERGED],
        residual_reason=f"target-specific landing proven on refs/heads/main at {TARGET_SHA}",
        human_action=None,
        local_work=[],
    )
    rendered = action_first_status(pr)
    assert "SOURCE: LANDED" in rendered
    assert "ACTIVE/RUNNING: UNKNOWN" in rendered
    assert "STATUS: ACTIVATION PENDING" in rendered
    assert "OPERATOR ACTION: NONE" in rendered
    assert "COMPLETION PROOF:" in rendered


def test_owned_worktree_cleanup_failure_is_deferable_not_merge_failure():
    assert LocalIntegrationCertificationMixin._cleanup_deferable(
        "cannot delete branch because it is checked out in worktree /tmp/dish"
    )
    assert not LocalIntegrationCertificationMixin._cleanup_deferable(
        "terminal agent branch is protected; cleanup refused"
    )


class _RecoveryGitHub:
    def list_prs(self):
        marker = (
            f"<!-- dish-integration-target-recovery:v1 source_pr=123 "
            f"source_head={HEAD} target=main -->"
        )
        return [
            {
                "number": 456,
                "body": marker,
                "head": {"sha": HEAD},
                "base": {"ref": "main"},
            }
        ]


def test_bound_target_recovery_pr_is_discovered_without_operator_relay():
    harness = object.__new__(LocalIntegrationCertificationMixin)
    harness.github = _RecoveryGitHub()
    current = SimpleNamespace(number=123, head=HEAD)
    recovery = harness._target_recovery_pr(current, "main")
    assert recovery["number"] == 456
