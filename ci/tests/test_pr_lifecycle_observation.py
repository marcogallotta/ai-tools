from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


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


class CountingAsana(ReadOnlyAsana):
    def __init__(self, *, completed=False, stories=None):
        super().__init__(completed=completed)
        self.task_reads = 0
        self.story_reads = 0
        self.story_values = list(stories or [])

    def list_project_tasks(self, project_gid):
        assert project_gid == PROJECT
        return [{
            "gid": TASK,
            "name": "Lifecycle observation",
            "notes": "",
            "completed": self.completed,
            "completed_at": NOW.isoformat() if self.completed else None,
            "modified_at": NOW.isoformat(),
            "memberships": [{"project": {"gid": PROJECT}, "section": {"gid": "section"}}],
            "dependencies": [],
        }]

    def get_task(self, gid):
        self.task_reads += 1
        return super().get_task(gid)

    def get_stories(self, gid):
        assert gid == TASK
        self.story_reads += 1
        return list(self.story_values)


class ReadOnlyEngine:
    def __init__(self, asana, *, candidate=None):
        self.asana = asana
        self.github = SimpleNamespace()
        self.candidate = candidate

    def now(self):
        return NOW

    def status(self, *, include_closed=False):
        assert include_closed is False
        return [lifecycle()]

    def _workstream_candidates(self, values):
        return {TASK: self.candidate} if self.candidate is not None else {}


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


def stacked_candidate():
    merged = SimpleNamespace(
        pr_number=170,
        head="b" * 40,
        branch="agent/stack-1",
        base="agent/stack-base",
        publication_state="merged",
        ultimate_target="main",
    )
    downstream = SimpleNamespace(
        pr_number=171,
        head=HEAD,
        branch="agent/observation-test",
        base="agent/stack-1",
        publication_state="open",
        ultimate_target="main",
    )
    return SimpleNamespace(
        workstream_task=TASK,
        candidate_id="candidate-id",
        shape_id="shape-id",
        source_complete=False,
        members=(merged, downstream),
    )


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


def test_configured_project_keeps_task_scope_when_no_pr_is_open():
    asana = ReadOnlyAsana(completed=False)

    tasks, scope = pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(asana),
        [],
        configured_projects=[PROJECT],
    )

    assert scope == {"status": "COMPLETE", "projects": [PROJECT]}
    assert [task["gid"] for task in tasks] == [TASK]
    assert tasks[0]["completed"] is False
    assert asana.writes == []


def test_configured_project_gid_fails_closed_when_malformed():
    with pytest.raises(pr_lifecycle.LifecycleError, match="invalid configured Asana observation project GID"):
        pr_lifecycle._task_observation_cycle(
            ReadOnlyEngine(ReadOnlyAsana()),
            [],
            configured_projects=["not-a-gid"],
        )


def test_warm_projection_reuses_complete_fingerprint_but_dirty_task_rereads():
    asana = CountingAsana()
    engine = ReadOnlyEngine(asana)
    first, _ = pr_lifecycle._task_observation_cycle(engine, [lifecycle()], bootstrap=True)
    assert (asana.task_reads, asana.story_reads) == (1, 1)

    warm, _ = pr_lifecycle._task_observation_cycle(
        engine, [lifecycle()], previous_tasks=first,
    )
    assert (asana.task_reads, asana.story_reads) == (1, 1)
    assert warm[0]["provenance"]["stories"] == "cached-complete-story-history"

    pr_lifecycle._task_observation_cycle(
        engine, [lifecycle()], previous_tasks=warm, dirty_task_gids=[TASK],
    )
    assert (asana.task_reads, asana.story_reads) == (2, 2)

    pr_lifecycle._task_observation_cycle(
        engine, [lifecycle()], previous_tasks=warm, bootstrap=True,
    )
    assert (asana.task_reads, asana.story_reads) == (3, 3)


def test_completed_project_row_is_retained_without_detail_or_story_reads():
    asana = CountingAsana(completed=True)
    tasks, _ = pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(asana), [lifecycle(completed=True)], bootstrap=True,
    )
    assert tasks[0]["completed"] is True
    assert tasks[0]["provenance"]["task"] == "asana-project-list-completed"
    assert (asana.task_reads, asana.story_reads) == (0, 0)


def test_warm_projection_recomputes_staleness_from_stable_attempt_basis():
    asana = CountingAsana(stories=[{
        "gid": "story-1",
        "created_at": (NOW - timedelta(hours=23)).isoformat(),
        "text": "DISPATCH ACCEPTED attempt_id=attempt-a",
    }])
    engine = ReadOnlyEngine(asana)
    first, _ = pr_lifecycle._task_observation_cycle(engine, [lifecycle()], bootstrap=True)
    assert first[0]["execution"]["stale"] is False
    engine.now = lambda: NOW + timedelta(hours=2)
    warm, _ = pr_lifecycle._task_observation_cycle(engine, [lifecycle()], previous_tasks=first)
    assert warm[0]["execution"]["stale"] is True
    assert warm[0]["execution"]["attempt_id"] == "attempt-a"
    assert (asana.task_reads, asana.story_reads) == (1, 1)


def test_active_task_budget_fails_before_any_detail_read():
    class TwoTaskAsana(CountingAsana):
        def list_project_tasks(self, project_gid):
            first = super().list_project_tasks(project_gid)[0]
            return [first, {**first, "gid": "1217593330664689"}]

    asana = TwoTaskAsana()
    with pytest.raises(pr_lifecycle.LifecycleError, match="active-task budget exceeded"):
        pr_lifecycle._task_observation_cycle(
            ReadOnlyEngine(asana), [lifecycle()], max_active_tasks=1,
        )
    assert (asana.task_reads, asana.story_reads) == (0, 0)


@pytest.mark.parametrize("change", ["notes", "membership", "dependency"])
def test_warm_fingerprint_change_forces_deep_read_without_modified_at_change(change):
    class ChangedListing(CountingAsana):
        def list_project_tasks(self, project_gid):
            value = super().list_project_tasks(project_gid)[0]
            if change == "notes":
                value["notes"] = "changed"
            elif change == "membership":
                value["memberships"][0]["section"]["gid"] = "different-section"
            else:
                value["dependencies"] = [{"gid": "1217593330664690", "completed": False}]
            return [value]

    original = CountingAsana()
    first, _ = pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(original), [lifecycle()], bootstrap=True,
    )
    changed = ChangedListing()
    pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(changed), [lifecycle()], previous_tasks=first,
    )
    assert (changed.task_reads, changed.story_reads) == (1, 1)


def test_removed_task_disappears_and_incompatible_cache_is_never_reused():
    asana = CountingAsana()
    first, _ = pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(asana), [lifecycle()], bootstrap=True,
    )
    broken = [{**first[0], "execution_basis": {"schema": "wrong"}}]
    pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(asana), [lifecycle()], previous_tasks=broken,
    )
    assert (asana.task_reads, asana.story_reads) == (2, 2)

    asana.list_project_tasks = lambda project_gid: []
    removed, _ = pr_lifecycle._task_observation_cycle(
        ReadOnlyEngine(asana), [lifecycle()], previous_tasks=first,
    )
    assert removed == []


def test_status_projection_consumes_asana_observation_without_write(tmp_path, monkeypatch):
    asana = ReadOnlyAsana(completed=False)
    engine = ReadOnlyEngine(asana)
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine, **kwargs: ({}, {}))
    args = SimpleNamespace(projection_path=tmp_path / "projection.json", repo="marcogallotta/ai-tools")

    pr_lifecycle._publish_projection(engine, [lifecycle()], args, mutate_tasks=False)

    payload = json.loads(args.projection_path.read_text(encoding="utf-8"))
    assert payload["task_scope"] == {"status": "COMPLETE", "projects": [PROJECT]}
    assert payload["tasks"][0]["gid"] == TASK
    assert payload["resolved_lifecycle"][0]["state"] == "READY_FOR_REVIEW"
    assert asana.writes == []


def test_status_projection_uses_configured_scope_after_last_pr_lands(tmp_path, monkeypatch):
    asana = ReadOnlyAsana(completed=False)

    class NoOpenPREngine(ReadOnlyEngine):
        def status(self, *, include_closed=False):
            assert include_closed is False
            return []

    engine = NoOpenPREngine(asana)
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine, **kwargs: ({}, {}))
    args = SimpleNamespace(
        projection_path=tmp_path / "projection.json",
        repo="marcogallotta/ai-tools",
        observation_project_gids=[PROJECT],
    )

    pr_lifecycle._publish_projection(engine, [], args, mutate_tasks=False)

    payload = json.loads(args.projection_path.read_text(encoding="utf-8"))
    assert payload["pull_requests"] == []
    assert payload["task_scope"] == {"status": "COMPLETE", "projects": [PROJECT]}
    assert [task["gid"] for task in payload["tasks"]] == [TASK]
    assert asana.writes == []


def test_mutating_projection_does_not_expand_writes_from_configured_scope(tmp_path, monkeypatch):
    asana = ReadOnlyAsana(completed=False)

    class NoOpenPREngine(ReadOnlyEngine):
        def status(self, *, include_closed=False):
            return []

    engine = NoOpenPREngine(asana)
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine, **kwargs: ({}, {}))
    args = SimpleNamespace(
        projection_path=tmp_path / "projection.json",
        repo="marcogallotta/ai-tools",
        observation_project_gids=[PROJECT],
    )

    pr_lifecycle._publish_projection(engine, [], args, mutate_tasks=True)

    payload = json.loads(args.projection_path.read_text(encoding="utf-8"))
    assert payload["task_scope"]["status"] == "UNKNOWN"
    assert payload["tasks"] == []
    assert asana.writes == []


def test_status_projection_refuses_authority_movement_during_long_scan(tmp_path, monkeypatch):
    asana = ReadOnlyAsana(completed=False)
    initial = lifecycle()
    moved = lifecycle(state=pr_lifecycle.LifecycleState.MERGED)

    class MovingEngine(ReadOnlyEngine):
        def status(self, *, include_closed=False):
            assert include_closed is False
            return [moved]

    engine = MovingEngine(asana)
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine: ({}, {}))
    path = tmp_path / "projection.json"
    path.write_text('{"generation":"previous-trustworthy"}\n', encoding="utf-8")
    args = SimpleNamespace(
        projection_path=path,
        repo="marcogallotta/ai-tools",
        include_closed=False,
    )

    with pytest.raises(pr_lifecycle.LifecycleError, match="authority changed during projection"):
        pr_lifecycle._publish_projection(engine, [initial], args, mutate_tasks=False)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "generation": "previous-trustworthy"
    }
    assert asana.writes == []


def test_failed_bootstrap_cache_converges_without_repeating_deep_reads(tmp_path, monkeypatch):
    asana = CountingAsana()
    initial = lifecycle()
    moved = lifecycle(state=pr_lifecycle.LifecycleState.MERGED)

    class MovingEngine(ReadOnlyEngine):
        def status(self, *, include_closed=False):
            return [moved]

    path = tmp_path / "projection.json"
    args = SimpleNamespace(
        projection_path=path,
        repo="marcogallotta/ai-tools",
        include_closed=False,
        observation_project_gids=[PROJECT],
        projection_bootstrap=True,
        projection_boot_id="boot-a",
        refresh_task_gids=[TASK],
        refresh_task_tokens=[f"{TASK}:7"],
    )
    monkeypatch.setattr(pr_lifecycle, "_source_observation_cycle", lambda engine, values: {})
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine, **kwargs: ({}, {}))

    with pytest.raises(pr_lifecycle.LifecycleError, match="authority changed during projection"):
        pr_lifecycle._publish_projection(MovingEngine(asana), [initial], args, mutate_tasks=False)
    assert (asana.task_reads, asana.story_reads) == (1, 1)
    cache = json.loads(pr_lifecycle._refresh_cache_path(path).read_text(encoding="utf-8"))
    assert cache["boot_id"] == "boot-a"
    assert cache["dirty_task_tokens"] == {TASK: 7}

    pr_lifecycle._publish_projection(ReadOnlyEngine(asana), [initial], args, mutate_tasks=False)
    assert (asana.task_reads, asana.story_reads) == (1, 1)
    assert path.exists()


def test_bootstrap_cache_is_rejected_by_a_different_service_boot(tmp_path, monkeypatch):
    asana = CountingAsana()
    initial = lifecycle()
    moved = lifecycle(state=pr_lifecycle.LifecycleState.MERGED)

    class MovingEngine(ReadOnlyEngine):
        def status(self, *, include_closed=False):
            return [moved]

    path = tmp_path / "projection.json"
    args = SimpleNamespace(
        projection_path=path,
        repo="marcogallotta/ai-tools",
        include_closed=False,
        observation_project_gids=[PROJECT],
        projection_bootstrap=True,
        projection_boot_id="boot-a",
        refresh_task_gids=[TASK],
        refresh_task_tokens=[f"{TASK}:7"],
    )
    monkeypatch.setattr(pr_lifecycle, "_source_observation_cycle", lambda engine, values: {})
    monkeypatch.setattr(pr_lifecycle, "_projection_health", lambda engine, **kwargs: ({}, {}))
    with pytest.raises(pr_lifecycle.LifecycleError, match="authority changed during projection"):
        pr_lifecycle._publish_projection(MovingEngine(asana), [initial], args, mutate_tasks=False)
    assert (asana.task_reads, asana.story_reads) == (1, 1)

    args.projection_boot_id = "boot-b"
    pr_lifecycle._publish_projection(ReadOnlyEngine(asana), [initial], args, mutate_tasks=False)
    assert (asana.task_reads, asana.story_reads) == (2, 2)


def test_budget_exhaustion_retains_previous_atomic_projection(tmp_path, monkeypatch):
    class SlowResponse:
        status = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b"{}"

    path = tmp_path / "projection.json"
    path.write_text('{"generation":"previous-trustworthy"}\n', encoding="utf-8")
    args = SimpleNamespace(projection_path=path, repo="marcogallotta/ai-tools")
    engine = ReadOnlyEngine(ReadOnlyAsana())
    budget = pr_lifecycle.ObservationBudget(max_requests=10, max_seconds=5)
    budget.started = budget.last_progress = 0
    client = pr_lifecycle.JSONHTTPClient(timeout=10, budget=budget)
    engine.github.http = client
    times = iter([0.0, 0.0, 0.0, 6.0])
    monkeypatch.setattr("pr_lifecycle_support.time.monotonic", lambda: next(times))
    monkeypatch.setattr("pr_lifecycle_support.urlrequest.urlopen", lambda *args, **kwargs: SlowResponse())
    def slow_final_read(engine, **kwargs):
        engine.github.http.request("GET", "https://example.invalid/final")
        return {}, {}
    monkeypatch.setattr(pr_lifecycle, "_projection_health", slow_final_read)

    with pytest.raises(pr_lifecycle.ObservationBudgetError, match="wall budget exhausted"):
        pr_lifecycle._publish_projection(
            engine, [lifecycle()], args, mutate_tasks=False,
        )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "generation": "previous-trustworthy"
    }


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


def test_local_implementation_completion_without_handoff_stays_implementation_required():
    projection = build_projection(
        [lifecycle(state=pr_lifecycle.LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED)],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("NOT_LANDED"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["phase"] == "IMPLEMENTATION_COMPLETION_REQUIRED"
    assert resolved["state"] == "IMPLEMENTATION_COMPLETION_REQUIRED"
    assert projection["queues"]["In Progress"] == [171]
    assert projection["queues"]["Integration"] == []


def test_local_implementation_completion_with_handoff_preserves_underlying_phase():
    projection = build_projection(
        [lifecycle(
            state=pr_lifecycle.LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED,
            human_action="give PR #171 to a local Implementation agent",
        )],
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=source("NOT_LANDED"),
    )

    resolved = projection["resolved_lifecycle"][0]
    assert resolved["phase"] == "IMPLEMENTATION_COMPLETION_REQUIRED"
    assert resolved["state"] == "BLOCKED_ON_MARCO"
    assert projection["queues"]["Decision"] == [171]
    assert projection["queues"]["Integration"] == []


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


def test_intermediate_merge_is_member_specific_in_real_workstream_projection():
    candidate = stacked_candidate()
    values = [lifecycle(state=pr_lifecycle.LifecycleState.REVIEW_READY)]
    observation = pr_lifecycle._source_observation_cycle(
        ReadOnlyEngine(None, candidate=candidate), values
    )

    assert observation["pull_requests"]["170"]["lineage_state"] == "MERGED_INTERMEDIATE_TARGET"
    assert "lineage_state" not in observation["pull_requests"]["171"]
    assert [member["publication_state"] for member in observation["workstreams"][0]["members"]] == [
        "merged", "open"
    ]

    projection = build_projection(
        values,
        repository="marcogallotta/ai-tools",
        tasks=[task(False)],
        source_observation=observation,
    )
    resolved = projection["resolved_lifecycle"][0]
    assert resolved["state"] == "READY_FOR_REVIEW"
    assert resolved["phase"] == "READY_FOR_REVIEW"
    assert projection["queues"]["Review"] == [171]
    assert projection["queues"]["Integration"] == []


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
