from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_projection import build_projection
from pr_lifecycle_rollout import (
    PROJECTION_START,
    commit_transition,
    install_plan,
    reconstruct,
    repair_projection,
    rollout_fence,
    rollout_projection,
)
from pr_lifecycle_support import LifecycleError, LifecycleState, PRLifecycle
from pr_lifecycle_task_state import ROLLOUT_PLAN_PREFIX


class FakeAsana:
    def __init__(self):
        self.tasks = {"task": {"gid": "task", "name": "rollout", "notes": "operator context\n", "modified_at": "m0"}}
        self.stories = {"task": []}
        self.concurrent_notes: str | None = None

    def get_task(self, gid):
        return json.loads(json.dumps(self.tasks[gid]))

    def get_stories(self, gid):
        return json.loads(json.dumps(self.stories[gid]))

    def add_comment(self, gid, text):
        item = {"gid": str(len(self.stories[gid]) + 1), "text": text}
        self.stories[gid].append(item)
        return item

    def update_projection_fields(self, gid, fields):
        self.tasks[gid].update(fields)
        self.tasks[gid]["modified_at"] = f"m{int(self.tasks[gid]['modified_at'][1:]) + 1}"
        if self.concurrent_notes is not None:
            self.tasks[gid]["notes"] = self.concurrent_notes
            self.concurrent_notes = None
        return self.get_task(gid)


def plan(*, generation=1, predecessor=None, artifact="artifact-a", config="config-a"):
    return {
        "plan_id": "release",
        "generation": generation,
        "predecessor_plan_digest": predecessor,
        "stages": [
            {"stage": "canary", "artifact": artifact, "config": config},
            {"stage": "production", "artifact": artifact, "config": config},
        ],
    }


def activation(stage="canary", *, generation=1, artifact="artifact-a", config="config-a", automatic=False):
    value = {
        "plan_id": "release", "generation": generation, "stage": stage,
        "artifact": artifact, "config": config, "event": "ACTIVATED",
        "automatic_effect": automatic,
    }
    if automatic:
        value["effect_mode"] = "idempotent-stable-key"
    return value


def decision(event, activated, *, stage="canary", generation=1, artifact="artifact-a", config="config-a", provenance="marco-chat-exact-decision"):
    return {
        "plan_id": "release", "generation": generation, "stage": stage,
        "artifact": artifact, "config": config, "event": event,
        "activated_identity": activated,
        "human_decision": {
            "decision": event, "plan_id": "release", "generation": generation,
            "stage": stage, "activated_identity": activated,
            "provenance": provenance, "source_id": "chat:exact-message-1",
        },
    }


def installed(tmp_path):
    asana = FakeAsana()
    installed_plan, changed = install_plan(asana, "task", plan(), fence_root=tmp_path)
    assert changed
    return asana, installed_plan


def test_real_local_task_plan_fence_rejects_second_committer(tmp_path):
    with rollout_fence("task", "release", root=tmp_path):
        with pytest.raises(LifecycleError, match="already active"):
            with rollout_fence("task", "release", root=tmp_path):
                pass


def test_plan_and_transition_are_append_only_idempotent_and_projected(tmp_path):
    asana, _ = installed(tmp_path)
    first, changed = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    assert changed and first["event"] == "ACTIVATED"
    replay, changed = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    assert not changed and replay == first
    state = reconstruct(asana.get_stories("task"), task_gid="task")
    assert len(state.plans) == 1 and len(state.transitions) == 1
    assert rollout_projection(state)["stages"][0]["state"] == "ACTIVATED"


def test_predecessor_and_human_decision_bind_exact_generation_stage_and_activation(tmp_path):
    asana, _ = installed(tmp_path)
    with pytest.raises(LifecycleError, match="predecessor"):
        commit_transition(asana, "task", activation("production"), fence_root=tmp_path)
    active, _ = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    stale = decision("ACCEPTED", active["transition_id"], generation=2)
    with pytest.raises(LifecycleError, match="stale generation"):
        commit_transition(asana, "task", stale, fence_root=tmp_path)
    wrong = decision("ACCEPTED", "wrong-activation")
    wrong["human_decision"]["activated_identity"] = "wrong-activation"
    with pytest.raises(LifecycleError, match="exact activated identity"):
        commit_transition(asana, "task", wrong, fence_root=tmp_path)
    accepted, changed = commit_transition(asana, "task", decision("ACCEPTED", active["transition_id"]), fence_root=tmp_path)
    assert changed and accepted["human_decision"]["source_id"] == "chat:exact-message-1"
    production, changed = commit_transition(asana, "task", activation("production"), fence_root=tmp_path)
    assert changed and production["event"] == "ACTIVATED"


def test_authenticated_account_label_is_not_human_provenance(tmp_path):
    asana, _ = installed(tmp_path)
    active, _ = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    request = decision("REJECTED", active["transition_id"], provenance="authenticated-account")
    with pytest.raises(LifecycleError, match="attribution alone"):
        commit_transition(asana, "task", request, fence_root=tmp_path)


def test_replacement_generation_supersedes_without_deleting_history(tmp_path):
    asana, first = installed(tmp_path)
    active, _ = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    commit_transition(asana, "task", decision("ACCEPTED", active["transition_id"]), fence_root=tmp_path)
    second_raw = plan(generation=2, predecessor=first["plan_digest"], artifact="artifact-b", config="config-b")
    second, changed = install_plan(asana, "task", second_raw, fence_root=tmp_path)
    assert changed and second["generation"] == 2
    state = reconstruct(asana.get_stories("task"), task_gid="task")
    projection = rollout_projection(state)
    assert len(state.plans) == 2 and projection["superseded_generations"] == [1]
    assert projection["complete"] is False and all(stage["state"] == "PENDING" for stage in projection["stages"])
    with pytest.raises(LifecycleError, match="stale generation"):
        commit_transition(asana, "task", activation(), fence_root=tmp_path)


def test_notes_overwrite_and_concurrent_projection_failure_recover_from_comments(tmp_path):
    asana, _ = installed(tmp_path)
    asana.tasks["task"]["notes"] = "unrelated operator rewrite\n"
    projection = repair_projection(asana, "task")
    assert projection["generation"] == 1
    assert "unrelated operator rewrite" in asana.tasks["task"]["notes"]
    asana.concurrent_notes = "concurrent writer won\n"
    with pytest.raises(LifecycleError, match="projection readback failed"):
        commit_transition(asana, "task", activation(), fence_root=tmp_path)
    # The authoritative transition was already appended. Replay repairs notes and
    # never duplicates the transition.
    transition, changed = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    assert not changed and transition["event"] == "ACTIVATED"
    assert "concurrent writer won" in asana.tasks["task"]["notes"]
    assert PROJECTION_START in asana.tasks["task"]["notes"]
    assert len(reconstruct(asana.get_stories("task"), task_gid="task").transitions) == 1


def test_crash_after_append_replays_without_duplicate_or_effect(tmp_path):
    asana, _ = installed(tmp_path)
    with pytest.raises(RuntimeError, match="injected crash"):
        commit_transition(asana, "task", activation(), fence_root=tmp_path, crash_after_append=True)
    transition, changed = commit_transition(asana, "task", activation(), fence_root=tmp_path)
    assert not changed and transition["event"] == "ACTIVATED"
    assert len(reconstruct(asana.get_stories("task"), task_gid="task").transitions) == 1


def test_marker_like_prose_is_ignored_but_exact_conflicting_authority_fails_closed(tmp_path):
    asana = FakeAsana()
    asana.add_comment("task", f"someone mentioned <!-- {ROLLOUT_PLAN_PREFIX} {{}} --> in prose")
    assert reconstruct(asana.get_stories("task"), task_gid="task").plans == ()
    installed_plan, _ = install_plan(asana, "task", plan(), fence_root=tmp_path)
    spoof = dict(installed_plan)
    spoof["stages"] = [{"stage": "canary", "artifact": "evil", "config": "evil"}]
    # Retaining the old digest makes the exact framed record non-canonical.
    from pr_lifecycle_task_state import structured_story
    asana.add_comment("task", structured_story(ROLLOUT_PLAN_PREFIX, spoof))
    with pytest.raises(LifecycleError, match="not canonical"):
        reconstruct(asana.get_stories("task"), task_gid="task")


def test_retry_safe_effect_requires_readback_and_recovers_after_effect_side_crash(tmp_path):
    asana, _ = installed(tmp_path)
    applied = set()
    attempts = []

    def effect(key):
        attempts.append(key)
        applied.add(key)
        if len(attempts) == 1:
            raise RuntimeError("crash after effect success")

    def readback(key):
        return key in applied

    request = activation(automatic=True)
    with pytest.raises(RuntimeError, match="effect success"):
        commit_transition(asana, "task", request, effect=effect, effect_readback=readback, fence_root=tmp_path)
    transition, changed = commit_transition(asana, "task", request, effect=effect, effect_readback=readback, fence_root=tmp_path)
    assert changed and len(attempts) == 2 and attempts[0] == attempts[1] == transition["transition_id"]
    replay, changed = commit_transition(asana, "task", request, effect=effect, effect_readback=readback, fence_root=tmp_path)
    assert not changed and replay == transition and len(attempts) == 2
    assert len(reconstruct(asana.get_stories("task"), task_gid="task").transitions) == 1


def test_effect_without_authoritative_readback_cannot_claim_activation(tmp_path):
    asana, _ = installed(tmp_path)
    with pytest.raises(LifecycleError, match="did not pass authoritative readback"):
        commit_transition(
            asana, "task", activation(automatic=True),
            effect=lambda key: None, effect_readback=lambda key: False, fence_root=tmp_path,
        )
    assert reconstruct(asana.get_stories("task"), task_gid="task").transitions == ()


def test_merge_or_task_completion_cannot_imply_rollout_completion(tmp_path):
    asana, _ = installed(tmp_path)
    asana.tasks["task"]["completed"] = True
    lifecycle = PRLifecycle(
        number=9, url="u", title="merged", head="a" * 40, branch="b", base="main", draft=False,
        state=LifecycleState.MERGED, state_label="MERGED", asana=[{"gid": "task", "completed": True}],
    )
    rollout = rollout_projection(reconstruct(asana.get_stories("task"), task_gid="task"))
    value = build_projection([lifecycle], repository="r", tasks=[{"gid": "task", "rollout": rollout}])
    assert value["pull_requests"][0]["state"] == "merged"
    assert value["rollouts"][0]["complete"] is False


def control_plan(*, generation=1, predecessor=None):
    return {
        "schema": "dish-rollout-plan-v2",
        "control_id": "guard-release",
        "plan_id": "guard-release",
        "generation": generation,
        "predecessor_plan_digest": predecessor,
        "stages": [
            {
                "stage": "OBSERVE",
                "artifact": "source-a",
                "config": "guard-observe-a",
                "target": "repo:marcogallotta/ai-tools profile:dish",
                "scope": "local guard sample",
                "activation_source": "file:dish/config/guard.json",
                "activation_method": "reload:dish-guard",
                "expected_prior_state": "OFF",
                "fence": "repo-profile:marcogallotta/ai-tools:dish",
                "authority": "Development Workflow rollout owner",
                "rollback": {
                    "state": "OFF",
                    "artifact": "source-a",
                    "config": "guard-off-a",
                    "activation_source": "file:dish/config/guard.json",
                    "activation_method": "reload:dish-guard",
                },
            },
            {
                "stage": "ASK",
                "artifact": "source-a",
                "config": "guard-ask-a",
                "target": "repo:marcogallotta/ai-tools profile:dish",
                "scope": "local guard sample",
                "activation_source": "file:dish/config/guard.json",
                "activation_method": "reload:dish-guard",
                "expected_prior_state": "OBSERVE",
                "fence": "repo-profile:marcogallotta/ai-tools:dish",
                "authority": "Development Workflow rollout owner",
                "rollback": {
                    "state": "OBSERVE",
                    "artifact": "source-a",
                    "config": "guard-observe-a",
                    "activation_source": "file:dish/config/guard.json",
                    "activation_method": "reload:dish-guard",
                },
            },
            {
                "stage": "ENFORCE",
                "artifact": "source-a",
                "config": "guard-enforce-a",
                "target": "repo:marcogallotta/ai-tools profile:dish",
                "scope": "local guard sample",
                "activation_source": "file:dish/config/guard.json",
                "activation_method": "reload:dish-guard",
                "expected_prior_state": "ASK",
                "fence": "repo-profile:marcogallotta/ai-tools:dish",
                "authority": "Development Workflow rollout owner",
                "rollback": {
                    "state": "ASK",
                    "artifact": "source-a",
                    "config": "guard-ask-a",
                    "activation_source": "file:dish/config/guard.json",
                    "activation_method": "reload:dish-guard",
                },
            },
        ],
    }


def control_transition(stage, *, event="ACTIVATED", activated_identity=None, prior_state=None, readback=None):
    configs = {
        "OBSERVE": "guard-observe-a",
        "ASK": "guard-ask-a",
        "ENFORCE": "guard-enforce-a",
    }
    if prior_state is None:
        prior_state = {"OBSERVE": "OFF", "ASK": "OBSERVE", "ENFORCE": "ASK"}[stage]
    value = {
        "plan_id": "guard-release",
        "generation": 1,
        "stage": stage,
        "artifact": "source-a",
        "config": configs[stage],
        "event": event,
        "prior_readback": {
            "target": "repo:marcogallotta/ai-tools profile:dish",
            "state": prior_state,
            "evidence": f"runtime-before:{prior_state}",
        },
    }
    if activated_identity:
        value["activated_identity"] = activated_identity
    if readback is None and event == "ACTIVATED":
        readback = {
            "target": "repo:marcogallotta/ai-tools profile:dish",
            "state": stage,
            "artifact": "source-a",
            "config": configs[stage],
            "activation_source": "file:dish/config/guard.json",
            "evidence": f"runtime-after:{stage}",
        }
    if readback is not None:
        value["readback"] = readback
    return value


def control_decision(event, activated, *, stage, config):
    return {
        "plan_id": "guard-release",
        "generation": 1,
        "stage": stage,
        "artifact": "source-a",
        "config": config,
        "event": event,
        "activated_identity": activated,
        "human_decision": {
            "decision": event,
            "plan_id": "guard-release",
            "generation": 1,
            "stage": stage,
            "activated_identity": activated,
            "provenance": "marco-chat-exact-decision",
            "source_id": f"chat:{stage.lower()}-{event.lower()}",
        },
    }


def test_v2_plan_requires_one_explicit_activation_source_and_fence(tmp_path):
    asana = FakeAsana()
    bad = control_plan()
    del bad["stages"][0]["activation_source"]
    with pytest.raises(LifecycleError, match="activation source"):
        install_plan(asana, "task", bad, fence_root=tmp_path)

    ambiguous = control_plan()
    ambiguous["stages"][0]["activation_source"] = ["file:a", "autosave:b"]
    with pytest.raises(LifecycleError, match="activation source"):
        install_plan(asana, "task", ambiguous, fence_root=tmp_path)


def test_v2_activation_requires_prior_fence_and_effective_state_readback(tmp_path):
    asana = FakeAsana()
    installed_plan, changed = install_plan(asana, "task", control_plan(), fence_root=tmp_path)
    assert changed and installed_plan["control_id"] == "guard-release"

    missing_prior = control_transition("OBSERVE")
    del missing_prior["prior_readback"]
    with pytest.raises(LifecycleError, match="prior-state readback"):
        commit_transition(asana, "task", missing_prior, fence_root=tmp_path)

    wrong_effective = control_transition("OBSERVE")
    wrong_effective["readback"]["config"] = "stale-config"
    with pytest.raises(LifecycleError, match="intended config"):
        commit_transition(asana, "task", wrong_effective, fence_root=tmp_path)

    active, changed = commit_transition(asana, "task", control_transition("OBSERVE"), fence_root=tmp_path)
    assert changed
    assert active["readback"]["state"] == "OBSERVE"
    projection = rollout_projection(reconstruct(asana.get_stories("task"), task_gid="task"))
    assert projection["schema"] == "dish-rollout-projection-v2"
    assert projection["control_id"] == "guard-release"
    assert projection["stages"][0]["effective_state"] == "OBSERVE"


def test_v2_progressive_guard_order_and_rollback_use_effective_readback(tmp_path):
    asana = FakeAsana()
    install_plan(asana, "task", control_plan(), fence_root=tmp_path)

    observe, _ = commit_transition(asana, "task", control_transition("OBSERVE"), fence_root=tmp_path)
    with pytest.raises(LifecycleError, match="predecessor stage is not accepted"):
        commit_transition(asana, "task", control_transition("ASK"), fence_root=tmp_path)
    commit_transition(
        asana, "task",
        control_decision("ACCEPTED", observe["transition_id"], stage="OBSERVE", config="guard-observe-a"),
        fence_root=tmp_path,
    )

    ask, _ = commit_transition(asana, "task", control_transition("ASK"), fence_root=tmp_path)
    commit_transition(
        asana, "task",
        control_decision("ACCEPTED", ask["transition_id"], stage="ASK", config="guard-ask-a"),
        fence_root=tmp_path,
    )

    enforce, _ = commit_transition(asana, "task", control_transition("ENFORCE"), fence_root=tmp_path)
    stale_runtime = control_transition(
        "ENFORCE",
        event="ROLLED_BACK",
        activated_identity=enforce["transition_id"],
        prior_state="ENFORCE",
        readback={
            "target": "repo:marcogallotta/ai-tools profile:dish",
            "state": "ENFORCE",
            "artifact": "source-a",
            "config": "guard-enforce-a",
            "activation_source": "file:dish/config/guard.json",
            "evidence": "source-restored-but-runtime-stale",
        },
    )
    with pytest.raises(LifecycleError, match="intended state"):
        commit_transition(asana, "task", stale_runtime, fence_root=tmp_path)

    rollback = control_transition(
        "ENFORCE",
        event="ROLLED_BACK",
        activated_identity=enforce["transition_id"],
        prior_state="ENFORCE",
        readback={
            "target": "repo:marcogallotta/ai-tools profile:dish",
            "state": "ASK",
            "artifact": "source-a",
            "config": "guard-ask-a",
            "activation_source": "file:dish/config/guard.json",
            "evidence": "runtime-after:ASK",
        },
    )
    rolled_back, changed = commit_transition(asana, "task", rollback, fence_root=tmp_path)
    assert changed and rolled_back["event"] == "ROLLED_BACK"
    projection = rollout_projection(reconstruct(asana.get_stories("task"), task_gid="task"))
    assert projection["stages"][2]["state"] == "ROLLED_BACK"
    assert projection["stages"][2]["effective_state"] == "ASK"
    assert projection["complete"] is False


def test_v2_automatic_effect_requires_structured_effective_readback(tmp_path):
    asana = FakeAsana()
    install_plan(asana, "task", control_plan(), fence_root=tmp_path)
    request = control_transition("OBSERVE")
    request["automatic_effect"] = True
    request["effect_mode"] = "target-fenced"
    request.pop("readback")

    with pytest.raises(LifecycleError, match="effective-state readback"):
        commit_transition(
            asana,
            "task",
            request,
            effect=lambda key: None,
            effect_readback=lambda key: True,
            fence_root=tmp_path,
        )

    observed = {
        "target": "repo:marcogallotta/ai-tools profile:dish",
        "state": "OBSERVE",
        "artifact": "source-a",
        "config": "guard-observe-a",
        "activation_source": "file:dish/config/guard.json",
        "evidence": "runtime-api:digest-123",
    }
    transition, changed = commit_transition(
        asana,
        "task",
        request,
        effect=lambda key: None,
        effect_readback=lambda key: observed,
        fence_root=tmp_path,
    )
    assert changed and transition["readback"] == observed


def test_v2_source_landing_does_not_claim_effective_activation(tmp_path):
    asana = FakeAsana()
    install_plan(asana, "task", control_plan(), fence_root=tmp_path)
    lifecycle = PRLifecycle(
        number=10, url="u", title="merged", head="b" * 40, branch="b", base="main", draft=False,
        state=LifecycleState.MERGED, state_label="MERGED", asana=[{"gid": "task", "completed": True}],
    )
    rollout = rollout_projection(reconstruct(asana.get_stories("task"), task_gid="task"))
    value = build_projection([lifecycle], repository="r", tasks=[{"gid": "task", "rollout": rollout}])

    assert value["pull_requests"][0]["state"] == "merged"
    assert value["rollouts"][0]["complete"] is False
    assert rollout["stages"][0]["state"] == "PENDING"
    assert rollout["stages"][0]["expected_prior_state"] == "OFF"
    assert rollout["stages"][0]["effective_state"] == "UNKNOWN"
