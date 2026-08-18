from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("pr_lifecycle_observation_subject", SCRIPTS / "pr_lifecycle.py")
assert SPEC and SPEC.loader
pr_lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pr_lifecycle
SPEC.loader.exec_module(pr_lifecycle)

from pr_lifecycle_projection import build_projection


NOW = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
TASK = "1217593330664688"
PROJECT = "1217419962189616"


def lifecycle(*, state=None, base="main", completed=False, leases=None, human_action=None):
    state = state or pr_lifecycle.LifecycleState.REVIEW_READY
    return pr_lifecycle.PRLifecycle(
        number=171,
        url="https://github.com/marcogallotta/ai-tools/pull/171",
        title="Observation test",
        head=HEAD,
        branch="agent/observation-test",
        base=base,
        draft=False,
        state=state,
        state_label=pr_lifecycle.STATE_LABELS[state],
        task_ids=[TASK],
        active_leases=list(leases or []),
        human_action=human_action,
        asana=[{
            "gid": TASK,
            "completed": completed,
            "memberships": [{"project": {"gid": PROJECT}, "section": {"gid": "section"}}],
        }],
    )


class ReadOnlyAsana:
    def __init__(self, *, completed=False):
        self.completed = completed
        self.writes = []

    def list_project_tasks(self, project_gid):
        assert project_gid == PROJECT
        return [{"gid": TASK}]

    def get_task(self, gid):
        assert gid == TASK
        return {
            "gid": TASK,
            "name": "Lifecycle observation",
            "completed": self.completed,
            "completed_at": NOW.isoformat() if self.completed else None,
            "modified_at": NOW.isoformat(),
            "memberships": [{"project": {"gid": PROJECT}, "section": {"gid": "section"}}],
            "dependencies": [],
            "dependents": [],
        }

    def get_stories(self, gid):
        assert gid == TASK
        return []

    def add_comment(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        raise AssertionError("pure observation must not write Asana")


class ReadOnlyEngine:
    def __init__(self, asana):
        self.asana = asana
        self.github = SimpleNamespace()

    def now(self):
        return NOW

    def _workstream_candidates(self, values):
        return {}


def task(completed, *, rollout=None):
    value = {"gid": TASK, "completed": completed, "modified_at": NOW.isoformat()}
    if rollout is not None:
        value["rollout"] = rollout
    return value


def source(state, **extra):
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "171": {
                "state": state,
                "ultimate_target": "main",
                "publication_state": extra.pop("publication_state", "open"),
                "provenance": "fixture",
                **extra,
            }
        },
        "workstreams": [],
    }


def runtime(*, active="UNKNOWN", operational="UNKNOWN"):
    return {
        "status": "COMPLETE",
        "pull_requests": {
            "171": {
                "active": active,
                "operational": operational,
                "provenance": "fixture-runtime",
            }
        },
    }


def accepted_rollout():
    return {
        "schema": "dish-rollout-projection-v1",
        "complete": True,
        "stages": [{"stage": "prod", "state": "ACCEPTED", "activated_identity": "activation"}],
    }


def test_pure_read_task_scan_returns_project_projection_without_writes():
    asana = ReadOnlyAsana(completed=False)
    values, scope = pr_lifecycle._task_observation_cycle(ReadOnlyEngine(asana), [lifecycle()])

    assert scope == {"status": "COMPLETE", "projects": [PROJECT]}
    assert values[0]["gid"] == TASK
    assert values[0]["completed"] is False
    assert values[0]["provenance"]["task"] == "asana-direct-read"
    assert asana.writes == []


def test_pure_read_task_scan_reports_unknown_scope_without_guessing():
    asana = ReadOnlyAsana()
    value = lifecycle()
    value.asana = []

    tasks, scope = pr_lifecycle._task_observation_cycle(ReadOnlyEngine(asana), [value])

    assert tasks == []
    assert scope["status"] == "UNKNOWN"
    assert scope["projects"] == []
    assert "scope" in scope["reason"]
    assert asana.writes == []


def test_status_projection_consumes_asana_observation_without_write(tmp_path, monkeypatch):
    asana = ReadOnlyAsana(completed=False)
    engine = ReadOnlyEngine(asana)
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine: ({}, {}))
    args = SimpleNamespace(projection_path=tmp_path / "projection.json", repo="marcogallotta/ai-tools")

    pr_lifecycle._publish_projection(engine, [lifecycle()], args, mutate_tasks=False)

    payload = json.loads(args.projection_path.read_text(encoding="utf-8"))
    assert payload["task_scope"] == {"status": "COMPLETE", "projects": [PROJECT]}
    assert payload["tasks"][0]["gid"] == TASK
    assert payload["resolved_lifecycle"][0]["state"] == "READY_FOR_REVIEW"
    assert asana.writes == []


def test_review_block_and_fix_in_progress_are_distinct_typed_states():
    blocked = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.CHANGES_REQUESTED)],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("NOT_LANDED"),
    )
    fixing = build_projection(
        [lifecycle(
            state=pr_lifecycle.LifecycleState.CHANGES_REQUESTED,
            leases=[{"phase": "fix", "head": HEAD}],
        )],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("NOT_LANDED"),
    )

    assert blocked["resolved_lifecycle"][0]["state"] == "REVIEW_BLOCK"
    assert fixing["resolved_lifecycle"][0]["state"] == "FIXES_IN_PROGRESS"


def test_local_implementation_completion_maps_to_review_pass_without_prose_inference():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED)],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("NOT_LANDED"),
    )

    assert projection["resolved_lifecycle"][0]["phase"] == "REVIEW_PASS"


def test_explicit_operator_action_is_typed_as_blocked_on_marco():
    projection = build_projection(
        [lifecycle(
            state=pr_lifecycle.LifecycleState.INTEGRATION_READY,
            human_action="operator decision required",
        )],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("NOT_LANDED"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "BLOCKED_ON_MARCO"
    assert resolved["phase"] == "INTEGRATION_READY"
    assert resolved["operator_action"] == "operator decision required"
    assert projection["queues"]["Decision"] == [171]


def test_intermediate_merge_is_not_laundered_into_main_landing():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.REVIEW_PASSED)],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source(
            "NOT_LANDED",
            publication_state="open",
            lineage_state="MERGED_INTERMEDIATE_TARGET",
        ),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "MERGED_INTERMEDIATE_TARGET"
    assert resolved["source"]["state"] == "NOT_LANDED"
    assert projection["queues"]["Integration"] == [171]


def test_landed_source_with_incomplete_asana_work_requires_post_merge_action():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.MERGED)],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("LANDED", publication_state="landed"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "POST_MERGE_ACTION_REQUIRED"
    assert resolved["completion"] == "INCOMPLETE"
    assert resolved["truth"] == "CONSISTENT"


def test_landed_source_without_runtime_witness_keeps_operational_unknown():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.MERGED, completed=True)],
        repository="marcogallotta/ai-tools",
        tasks=[task(True, rollout=accepted_rollout())],
        source_observation=source("LANDED", publication_state="landed"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "LANDED_ON_MAIN"
    assert resolved["runtime"]["active"] == "UNKNOWN"
    assert resolved["runtime"]["operational"] == "UNKNOWN"
    assert projection["pull_requests"][0]["active_state"] == "UNKNOWN"
    assert projection["pull_requests"][0]["operational_state"] == "UNKNOWN"
    assert "Operational Unknown" in projection["pull_requests"][0]["state_label"]


def test_operational_completion_requires_explicit_runtime_witness():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.MERGED, completed=True)],
        repository="marcogallotta/ai-tools",
        tasks=[task(True, rollout=accepted_rollout())],
        source_observation=source("LANDED", publication_state="landed"),
        runtime_observation=runtime(active="OBSERVED", operational="OPERATIONAL"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "OPERATIONALLY_COMPLETE"
    assert resolved["runtime"]["active"] == "OBSERVED"
    assert resolved["runtime"]["operational"] == "OPERATIONAL"
    assert projection["pull_requests"][0]["operational_state"] == "OPERATIONAL"


def test_runtime_not_operational_requires_post_merge_action():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.MERGED, completed=True)],
        repository="marcogallotta/ai-tools",
        tasks=[task(True, rollout=accepted_rollout())],
        source_observation=source("LANDED", publication_state="landed"),
        runtime_observation=runtime(active="OBSERVED", operational="NOT_OPERATIONAL"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "POST_MERGE_ACTION_REQUIRED"
    assert resolved["runtime"]["operational"] == "NOT_OPERATIONAL"


def test_missing_asana_work_state_is_explicit_unknown_in_dashboard_projection():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.REVIEW_READY)],
        repository="marcogallotta/ai-tools",
        tasks=[],
        source_observation=source("NOT_LANDED"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["truth"] == "UNKNOWN"
    assert resolved["conflicts"][0]["kind"] == "ASANA_WORK_STATE_UNKNOWN"
    assert projection["pull_requests"][0]["state_label"].endswith("Truth Unknown")
    assert projection["state_drift"][0]["conflict"] == "ASANA_WORK_STATE_UNKNOWN"


def test_conflicting_asana_completion_and_github_landing_is_explicit():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.REVIEW_READY, completed=True)],
        repository="marcogallotta/ai-tools",
        tasks=[task(True)],
        source_observation=source("NOT_LANDED"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "CONTRADICTION"
    assert resolved["truth"] == "CONTRADICTION"
    assert resolved["conflicts"][0]["kind"] == "ASANA_COMPLETE_SOURCE_NOT_LANDED"
    assert projection["state_drift"][0]["repair_owner"] == "authority-owner"
    assert projection["queues"]["Blocked"] == [171]
