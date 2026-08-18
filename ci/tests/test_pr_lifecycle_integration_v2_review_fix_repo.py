from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_integration_certification import LocalIntegrationCertificationMixin, TARGET_RECOVERY_MARKER
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_projection import build_projection
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

    def __init__(self, recovery, *, post_merge_gates="NONE"):
        self.http = FakeHTTP()
        self.recovery = recovery
        self.post_merge_gates = post_merge_gates
        self.include_closed_calls = []

    def _url(self, path):
        return "https://github.invalid/" + path

    def get_ref_sha(self, ref):
        assert ref == "heads/main"
        return TARGET_SHA

    def list_prs(self, *, include_closed=False):
        self.include_closed_calls.append(include_closed)
        return [self.recovery]

    def get_reviews(self, number):
        assert number == 10
        return [
            {
                "id": 1,
                "state": "COMMENTED",
                "commit_id": SOURCE_HEAD,
                "submitted_at": "2026-08-18T00:00:00Z",
                "body": (
                    "VERDICT: MERGE\n\n"
                    "PRE-INTEGRATION TESTS TO RUN: NONE\n"
                    f"POST-MERGE GATES: {self.post_merge_gates}"
                ),
            }
        ]


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


def merged_lifecycle(*, rollout_value, activation_requirement="required", evidence="post-merge activation required", residual=None):
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
        asana=[{
            "gid": "task",
            "rollout": rollout_value,
            "activation_requirement": activation_requirement,
            "activation_requirement_evidence": evidence,
        }],
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


def runtime(*, operational="OPERATIONAL", generation=3, activated_identity="activation-123"):
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "10": {
                "active": "OBSERVED",
                "operational": operational,
                "generation": generation,
                "activated_identity": activated_identity,
                "provenance": "focused-runtime-witness",
            }
        },
    }


def landed_source():
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "10": {
                "state": "LANDED",
                "ultimate_target": "main",
                "publication_state": "landed",
                "provenance": "focused-target-readback",
            }
        },
        "workstreams": [],
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
    assert lifecycle.asana[0]["activation_requirement"] == "not-required"
    assert github.include_closed_calls == [True, True]


def test_exact_review_post_merge_gate_marks_no_plan_as_activation_required():
    github = FakeGitHub(
        recovery_pr(),
        post_merge_gates="Asana task 1217519197662916 — staged rollout activation/acceptance",
    )
    lifecycle = Harness(github, asana=FakeAsana()).inspect(source_pr())
    task = lifecycle.asana[0]
    assert task["rollout"] is None
    assert task["activation_requirement"] == "required"
    assert "staged rollout activation/acceptance" in task["activation_requirement_evidence"]


def test_merged_without_rollout_but_activation_required_stays_pending():
    rendered = action_first_status(
        merged_lifecycle(
            rollout_value=None,
            activation_requirement="required",
            evidence="exact-head Review requires post-merge gate: staged rollout activation/acceptance",
        )
    )
    assert "STATUS: ACTIVATION PENDING" in rendered
    assert "ACTIVE/RUNNING: UNKNOWN" in rendered
    assert "no rollout plan/readback exists yet" in rendered
    assert "STATUS: OPERATIONAL" not in rendered


def test_merged_without_rollout_is_operational_only_with_explicit_no_activation_gate():
    rendered = action_first_status(
        merged_lifecycle(
            rollout_value=None,
            activation_requirement="not-required",
            evidence="exact-head Review explicitly declares POST-MERGE GATES: NONE",
        )
    )
    assert "STATUS: OPERATIONAL" in rendered
    assert "ACTIVE/RUNNING: NOT REQUIRED" in rendered
    assert "POST-MERGE GATES: NONE" in rendered
    assert "ACTIVATION PENDING" not in rendered


def test_accepted_rollout_without_current_runtime_witness_is_not_operational():
    rendered = action_first_status(merged_lifecycle(rollout_value=projection("ACCEPTED", complete=True)))
    assert "STATUS: VERIFYING" in rendered
    assert "STATUS: OPERATIONAL" not in rendered
    assert "current runtime witness does not prove the expected active generation/identity" in rendered
    assert "expected 'activation-123'" in rendered


def test_accepted_rollout_with_matching_current_runtime_witness_is_operational():
    rendered = action_first_status(
        merged_lifecycle(rollout_value=projection("ACCEPTED", complete=True)),
        runtime(),
    )
    assert "STATUS: OPERATIONAL" in rendered
    assert "current runtime witness proves generation 3 activated identity activation-123 OPERATIONAL" in rendered


def test_accepted_rollout_with_stale_runtime_identity_stays_verifying():
    rendered = action_first_status(
        merged_lifecycle(rollout_value=projection("ACCEPTED", complete=True)),
        runtime(operational="UNKNOWN", generation=2, activated_identity="activation-old"),
    )
    assert "STATUS: VERIFYING" in rendered
    assert "STATUS: OPERATIONAL" not in rendered
    assert "expected 3" in rendered
    assert "expected 'activation-123'" in rendered


def test_direct_operator_and_read_only_projection_agree_on_activation_required_operational_truth():
    lifecycle = merged_lifecycle(rollout_value=projection("ACCEPTED", complete=True))
    tasks = [{"gid": "task", "completed": True, "rollout": projection("ACCEPTED", complete=True)}]

    without_runtime = build_projection(
        [lifecycle],
        repository="marcogallotta/ai-tools",
        tasks=tasks,
        source_observation=landed_source(),
    )
    rendered = action_first_status(lifecycle)
    assert without_runtime["resolved_lifecycle"][0]["state"] != "OPERATIONALLY_COMPLETE"
    assert "STATUS: OPERATIONAL" not in rendered

    witness = runtime()
    with_runtime = build_projection(
        [lifecycle],
        repository="marcogallotta/ai-tools",
        tasks=tasks,
        source_observation=landed_source(),
        runtime_observation=witness,
    )
    rendered = action_first_status(lifecycle, witness)
    assert with_runtime["resolved_lifecycle"][0]["state"] == "OPERATIONALLY_COMPLETE"
    assert "STATUS: OPERATIONAL" in rendered


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
