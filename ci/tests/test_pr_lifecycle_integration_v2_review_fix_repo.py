from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_integration_certification import LocalIntegrationCertificationMixin, TARGET_RECOVERY_MARKER
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_support import LifecycleState, PRLifecycle


SOURCE_EFFECT = "a" * 40
TARGET_SHA = "b" * 40
RECOVERY_EFFECT = "c" * 40
SOURCE_HEAD = "d" * 40
RECOVERY_HEAD = "e" * 40


class FakeHTTP:
    def request(self, method, url, *, headers=None):
        if f"compare/{SOURCE_EFFECT}...{TARGET_SHA}" in url:
            return 200, {}, {"status": "diverged"}
        if f"compare/{RECOVERY_EFFECT}...{TARGET_SHA}" in url:
            return 200, {}, {"status": "ahead"}
        raise AssertionError(url)


class FakeGitHub:
    repository = "marcogallotta/ai-tools"
    headers = {}

    def __init__(self, recovery):
        self.http = FakeHTTP()
        self.recovery = recovery
        self.include_closed_calls = []

    def _url(self, path):
        return "https://github.invalid/" + path

    def get_ref_sha(self, ref):
        assert ref == "heads/main"
        return TARGET_SHA

    def list_prs(self, *, include_closed=False):
        self.include_closed_calls.append(include_closed)
        return [self.recovery]


class FakeAsana:
    def __init__(self, stories=None):
        self.stories = list(stories or [])

    def get_stories(self, gid):
        assert gid == "task"
        return list(self.stories)


class BaseInspect:
    def inspect(self, pr):
        return PRLifecycle(
            number=int(pr["number"]),
            url="u",
            title="t",
            head=SOURCE_HEAD,
            branch="feature",
            base="feature-base",
            draft=False,
            state=LifecycleState.MERGED,
            state_label="MERGED",
            task_ids=["task"],
            asana=[{"gid": "task"}],
        )


class Harness(LocalIntegrationCertificationMixin, BaseInspect):
    def __init__(self, github, asana=None):
        self.github = github
        self.asana = asana


def recovery_pr():
    marker = f"<!-- {TARGET_RECOVERY_MARKER} source_pr=10 source_head={SOURCE_HEAD} target=main -->"
    return {
        "number": 11,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-08-18T00:00:00Z",
        "merge_commit_sha": RECOVERY_EFFECT,
        "body": marker,
        "base": {"ref": "main"},
        "head": {"sha": RECOVERY_HEAD},
    }


def source_pr():
    return {
        "number": 10,
        "merged": True,
        "merged_at": "2026-08-18T00:00:00Z",
        "merge_commit_sha": SOURCE_EFFECT,
        "head": {"sha": SOURCE_HEAD},
        "base": {"ref": "feature-base", "repo": {"default_branch": "main"}},
    }


def merged_lifecycle(*, rollout_value, residual=None):
    return PRLifecycle(
        number=10,
        url="u",
        title="t",
        head=SOURCE_HEAD,
        branch="feature",
        base="main",
        draft=False,
        state=LifecycleState.MERGED,
        state_label="MERGED",
        task_ids=["task"],
        asana=[{"gid": "task", "rollout": rollout_value}],
        residual_reason=residual,
    )


def projection(state, *, complete=False, generation=3):
    return {
        "plan_id": "release",
        "generation": generation,
        "complete": complete,
        "stages": [
            {
                "stage": "production",
                "artifact": "artifact-x",
                "config": "config-y",
                "state": state,
                "activated_identity": "activation-123" if state in {"ACTIVATED", "ACCEPTED"} else None,
            }
        ],
    }


def test_closed_squash_recovery_is_durable_target_landing_and_not_recreated():
    github = FakeGitHub(recovery_pr())
    engine = Harness(github, asana=FakeAsana())
    proof = engine._target_landing_proof(source_pr())
    assert proof["landed"] is True
    assert proof["recovery_pr"] == 11
    assert proof["effect_sha"] == RECOVERY_EFFECT
    assert proof["source_effect_sha"] == SOURCE_EFFECT
    assert github.include_closed_calls == [True]

    lifecycle = engine.inspect(source_pr())
    assert lifecycle.state == LifecycleState.MERGED
    assert lifecycle.asana[0]["rollout"] is None
    assert github.include_closed_calls == [True, True]


def test_merged_without_declared_rollout_is_operational_not_activation_pending():
    rendered = action_first_status(merged_lifecycle(rollout_value=None))
    assert "STATUS: OPERATIONAL" in rendered
    assert "ACTIVE/RUNNING: NOT REQUIRED" in rendered
    assert "ACTIVATION PENDING" not in rendered


def test_merged_with_accepted_final_rollout_stage_is_operational_with_exact_proof():
    rendered = action_first_status(merged_lifecycle(rollout_value=projection("ACCEPTED", complete=True)))
    assert "STATUS: OPERATIONAL" in rendered
    assert "generation 3 final stage production ACCEPTED" in rendered
    assert "activation-123" in rendered


def test_merged_with_activated_unaccepted_stage_is_verifying():
    rendered = action_first_status(merged_lifecycle(rollout_value=projection("ACTIVATED")))
    assert "STATUS: VERIFYING" in rendered
    assert "ACTIVATED" in rendered


def test_merged_with_pending_rollout_retains_activation_pending():
    rendered = action_first_status(merged_lifecycle(rollout_value=projection("PENDING")))
    assert "STATUS: ACTIVATION PENDING" in rendered
    assert "NOT YET PROVEN" in rendered


def test_missing_authoritative_task_rollout_readback_does_not_assume_no_activation():
    value = merged_lifecycle(rollout_value=None)
    value.asana = [{"gid": "task"}]
    rendered = action_first_status(value)
    assert "STATUS: ACTIVATION PENDING" in rendered
    assert "authoritative rollout reconstruction is unavailable" in rendered
