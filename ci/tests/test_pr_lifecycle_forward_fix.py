from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_ci_recovery import recover_failed_ci, BASELINE_OWNER_MARKER
from pr_lifecycle_projection import atomic_write, build_projection, read_projection
from pr_lifecycle_support import LifecycleError, LifecycleState, PRLifecycle
from pr_lifecycle_task_state import apply_transition, execution_truth, task_snapshot
import pr_lifecycle_controller as controller
import pr_gate


class FakeGitHub:
    repository = "marcogallotta/ai-tools"
    def __init__(self):
        self.comments = []
        self.retries = []
    def get_comments(self, number): return list(self.comments)
    def rerun_failed_workflow(self, run_id): self.retries.append(run_id)
    def add_comment(self, number, body):
        item = {"id": len(self.comments)+1, "body": body, "created_at": datetime.now(timezone.utc).isoformat()}
        self.comments.append(item); return item
    def get_pr(self, number): return {"number": number}


class FakeAsana:
    def __init__(self):
        self.tasks = {}
        self.stories = {}
        self.project_tasks = {}
        self.moves = []
    def get_task(self, gid): return json.loads(json.dumps(self.tasks[gid]))
    def get_stories(self, gid): return list(self.stories.get(gid, []))
    def add_comment(self, gid, text):
        story = {"id": len(self.stories.get(gid, []))+1, "text": text, "created_at": datetime.now(timezone.utc).isoformat()}
        self.stories.setdefault(gid, []).append(story); return story
    def update_projection_fields(self, gid, fields):
        self.tasks[gid].update(fields)
        self.tasks[gid]["modified_at"] = datetime.now(timezone.utc).isoformat()
        return self.get_task(gid)
    def move_task_to_section(self, gid, section_gid):
        self.moves.append((gid, section_gid))
        self.tasks[gid]["memberships"] = [{"project":{"gid":"p"},"section":{"gid":section_gid}}]
    def list_project_tasks(self, project_gid): return [self.get_task(gid) for gid in self.project_tasks.get(project_gid, [])]
    def create_task(self, project_gid, *, name, notes):
        gid = str(1217561810889999 + len(self.tasks))
        self.tasks[gid] = {"gid":gid,"name":name,"notes":notes,"completed":False,"modified_at":"t","memberships":[{"project":{"gid":project_gid},"section":{"gid":"ready"}}],"dependencies":[]}
        self.project_tasks.setdefault(project_gid, []).append(gid)
        return self.get_task(gid)
    def find_task_by_marker(self, project_gid, marker_name, exact_marker):
        matches=[t for t in self.list_project_tasks(project_gid) if marker_name in t.get("notes","") and exact_marker in t.get("notes","")]
        if len(matches)>1: raise LifecycleError("duplicate")
        return matches[0] if matches else None


class FakeEngine:
    def __init__(self, current, *, asana=None, now=None):
        self.github=FakeGitHub(); self.asana=asana; self.current=current
        self._now=now or datetime(2026,8,17,20,tzinfo=timezone.utc)
    def now(self): return self._now
    def inspect(self, raw): return self.current


def lifecycle(classification: str, *, evidence="proof", run_id=123, main_sha=None):
    gate={
        "diagnosis": pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value,
        "failure_ownership":classification,
        "failure_ownership_evidence":evidence,
        "required_workflow_run_id":run_id,
        "required_check":pr_gate.REQUIRED_ORDINARY_CI_CONTEXT,
    }
    if main_sha: gate["failure_main_sha"]=main_sha
    return PRLifecycle(number=8,url="u",title="t",head="a"*40,branch="b",base="main",draft=False,
        state=LifecycleState.REVIEW_PASSED,state_label="review",task_ids=["1217561810880370"],gate=gate)


def test_controller_keeps_proven_watcher_and_adds_projection():
    paths=controller._paths(Path("/tmp/controller-test"))
    command=controller._watcher_command(paths)
    assert command[-6:]==["watch","--dispatch","--interval","180","--format","table"]
    assert command[command.index("--http-timeout")+1]=="10"
    assert command[command.index("--projection-path")+1]==str(paths["projection"])
    assert "--integration-authority" in command


def test_infrastructure_retries_twice_then_waits_and_probes():
    current=lifecycle("INFRASTRUCTURE")
    engine=FakeEngine(current)
    recover_failed_ci(engine,current); recover_failed_ci(engine,current)
    assert engine.github.retries == [123,123]
    waited=recover_failed_ci(engine,current)
    assert waited.state is LifecycleState.WAITING_INFRASTRUCTURE
    assert engine.github.retries == [123,123,123]  # first capability probe
    # Another pass inside 15 minutes does not probe again.
    recover_failed_ci(engine,current)
    assert engine.github.retries == [123,123,123]
    assert all("source mutation" not in c["body"].lower() or "no source mutation" in c["body"].lower() for c in engine.github.comments)


def test_ambiguous_failure_never_routes_as_infrastructure_or_main():
    current=lifecycle("AMBIGUOUS")
    engine=FakeEngine(current)
    assert recover_failed_ci(engine,current) is current
    assert engine.github.retries == []


def test_current_main_failure_dedupes_one_owner_and_fans_out():
    main="b"*40
    asana=FakeAsana()
    task={"gid":"1217561810880370","name":"stage2","notes":"","completed":False,"modified_at":"t","memberships":[{"project":{"gid":"proj"},"section":{"gid":"ready"}}],"dependencies":[]}
    asana.tasks[task["gid"]]=task; asana.project_tasks["proj"]=[task["gid"]]
    current=lifecycle("PROVEN_CURRENT_MAIN", main_sha=main)
    current.asana=[task]
    engine=FakeEngine(current, asana=asana)
    recover_failed_ci(engine,current)
    owners=[t for t in asana.tasks.values() if BASELINE_OWNER_MARKER in t.get("notes","")]
    assert len(owners)==1
    # Same defect on another candidate in same project reuses the owner.
    engine.current=current
    recover_failed_ci(engine,current)
    owners=[t for t in asana.tasks.values() if BASELINE_OWNER_MARKER in t.get("notes","")]
    assert len(owners)==1
    assert any("dish-external-dependency:v1" in c["body"] for c in engine.github.comments)


def test_task_transition_exact_precondition_idempotent_and_readback():
    a=FakeAsana(); gid="1"
    a.tasks[gid]={"gid":gid,"name":"Ready","notes":"STATE: READY\n","completed":False,"modified_at":"m1","memberships":[{"project":{"gid":"p"},"section":{"gid":"s1"}}],"dependencies":[]}
    expected=task_snapshot(a.get_task(gid))
    desired={"name":"In Progress","notes":"STATE: IN PROGRESS\n","section":"s2"}
    first=apply_transition(a,gid,expected=expected,desired=desired,kind="projection-repair")
    assert first.changed and a.tasks[gid]["name"]=="In Progress" and a.moves[-1]==(gid,"s2")
    # Replay uses the durable transition id even though the task's modified_at changed.
    assert not apply_transition(a,gid,expected=expected,desired=desired,kind="projection-repair").changed
    # Exact current state + same stable identity is idempotent when expected is current.
    expected2=task_snapshot(a.get_task(gid)); desired2={"completed":False}
    x=apply_transition(a,gid,expected=expected2,desired=desired2,kind="terminal-repair")
    y=apply_transition(a,gid,expected=expected2,desired=desired2,kind="terminal-repair")
    assert x.changed and not y.changed


def test_transition_dependency_gate_is_stage_specific():
    a=FakeAsana(); gid="1"
    a.tasks[gid]={"gid":gid,"name":"Ready","notes":"","completed":False,"modified_at":"m1","memberships":[],"dependencies":[{"gid":"dep","completed":False}]}
    expected=task_snapshot(a.get_task(gid))
    with pytest.raises(LifecycleError, match="blocked by incomplete dependencies"):
        apply_transition(a,gid,expected=expected,desired={"name":"Running"},kind="dispatch-request")
    # Projection repair is allowed despite an unrelated execution dependency.
    assert apply_transition(a,gid,expected=expected,desired={"name":"Ready"},kind="projection-repair").changed


def test_execution_truth_does_not_upgrade_handoff_to_running():
    now=datetime(2026,8,17,21,tzinfo=timezone.utc)
    handoff=[{"text":"HANDOFF PREPARED — user relay required","created_at":(now-timedelta(minutes=5)).isoformat()}]
    assert execution_truth({},handoff,now=now)["state"]=="HANDOFF RECORDED"
    accepted=handoff+[{"text":"DESTINATION ACCEPTED / BOUND","created_at":now.isoformat()}]
    assert execution_truth({},accepted,now=now)["state"]=="DISPATCH ACCEPTED / BOUND"
    running=accepted+[{"text":"RUNNING-SOURCE — commit exists","created_at":(now+timedelta(seconds=1)).isoformat()}]
    assert execution_truth({},running,now=now+timedelta(seconds=1))["state"]=="RUNNING-SOURCE"


def test_old_handoff_becomes_stale_execution_unknown():
    now=datetime(2026,8,17,21,tzinfo=timezone.utc)
    stories=[{"text":"HANDOFF RECORDED","created_at":(now-timedelta(hours=2)).isoformat()}]
    truth=execution_truth({},stories,now=now)
    assert truth["stale"] and truth["state"]=="STALE / EXECUTION UNKNOWN"


def test_projection_is_atomic_normalized_and_surfaces_drift(tmp_path):
    pr=PRLifecycle(number=1,url="u",title="t",head="a"*40,branch="b",base="main",draft=False,
        state=LifecycleState.MERGED,state_label="MERGED",asana=[{"gid":"2","completed":False}])
    value=build_projection([pr],repository="r",tasks=[{"gid":"2","execution":{"state":"HANDOFF RECORDED"}}])
    assert value["queues"]["Recent"]==[1]
    assert value["state_drift"] and value["state_drift"][0]["repair_owner"]=="controller"
    path=tmp_path/"lifecycle.json"; atomic_write(path,value)
    assert read_projection(path)["pull_requests"][0]["head"]=="a"*40
    assert not list(tmp_path.glob(".*.tmp"))
