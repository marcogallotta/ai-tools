from __future__ import annotations

from copy import deepcopy

import test_pr_lifecycle as base
from pr_lifecycle_asana_writeback import reconcile_after_merge

p = base.pr_lifecycle
TASK = "1217443403986570"
DEPENDENT = "1217443403986571"
MERGE_SHA = "d" * 40


class FakeAsana:
    def __init__(self, owner, dependent=None, *, concurrent_note=None):
        self.tasks = {TASK: deepcopy(owner)}
        if dependent is not None:
            self.tasks[DEPENDENT] = deepcopy(dependent)
        self.stories = {TASK: []}
        self.field_updates = []
        self.comments = []
        self.concurrent_note = concurrent_note

    def get_task(self, gid):
        return deepcopy(self.tasks[gid])

    def get_stories(self, gid):
        return deepcopy(self.stories.get(gid, []))

    def add_comment(self, gid, text):
        self.comments.append((gid, text))
        self.stories.setdefault(gid, []).append({"text": text})
        if self.concurrent_note is not None:
            self.tasks[gid]["notes"] += "\n" + self.concurrent_note
            self.concurrent_note = None
        return {"gid": "story-1", "text": text}

    def update_task_fields(self, gid, fields):
        assert "notes" not in fields and "html_notes" not in fields
        self.field_updates.append((gid, dict(fields)))
        self.tasks[gid].update(fields)
        return deepcopy(self.tasks[gid])

    def remove_dependency(self, task_gid, dependency_gid):
        task = self.tasks[task_gid]
        task["dependencies"] = [x for x in task.get("dependencies", []) if x.get("gid") != dependency_gid]


def merged_lifecycle(*, dependents=None):
    task_ids = p.task_ids_from_pr(base.pr(body=f"Owning task: {TASK}"))
    return p.PRLifecycle(
        number=31,
        url="https://github.com/marcogallotta/ai-tools/pull/31",
        title="merged",
        head=base.HEAD,
        branch="agent/test",
        base="main",
        draft=False,
        state=p.LifecycleState.MERGED,
        state_label=p.STATE_LABELS[p.LifecycleState.MERGED],
        task_ids=task_ids,
    )


def reconcile(asana):
    return reconcile_after_merge(
        asana=asana,
        lifecycle=merged_lifecycle(),
        repository="marcogallotta/ai-tools",
        merge_sha=MERGE_SHA,
    )


def test_post_merge_records_exact_landing_without_replacing_task_notes():
    notes = "source task\n<!-- dish-residual-gate:v1 kind=runtime state=pending -->"
    asana = FakeAsana({"gid": TASK, "notes": notes, "completed": False, "dependents": []})
    result = reconcile(asana)
    assert result.landing_recorded is True
    assert result.completed is False
    assert result.residual_gates == ("runtime",)
    assert asana.tasks[TASK]["notes"] == notes
    assert asana.field_updates == []
    assert f"pr=31 head={base.HEAD} merge={MERGE_SHA}" in asana.comments[0][1]


def test_source_task_completes_only_when_explicitly_final_and_no_residual_gate():
    asana = FakeAsana({
        "gid": TASK,
        "notes": "<!-- dish-source-work:v1 final_outstanding_gate=true -->",
        "completed": False,
        "dependents": [],
    })
    result = reconcile(asana)
    assert result.completed is True
    assert asana.field_updates == [(TASK, {"completed": True})]


def test_concurrent_residual_note_after_landing_comment_prevents_stale_completion():
    asana = FakeAsana(
        {
            "gid": TASK,
            "notes": "<!-- dish-source-work:v1 final_outstanding_gate=true -->",
            "completed": False,
            "dependents": [],
        },
        concurrent_note="<!-- dish-residual-gate:v1 kind=deployment state=open -->",
    )
    result = reconcile(asana)
    assert result.completed is False
    assert result.residual_gates == ("deployment",)
    assert asana.field_updates == []


def test_landing_comment_is_idempotent_after_recovery():
    asana = FakeAsana({"gid": TASK, "notes": "", "completed": False, "dependents": []})
    first = reconcile(asana)
    second = reconcile(asana)
    assert first.landing_recorded and second.landing_recorded
    assert len(asana.comments) == 1


def test_dependent_advances_only_when_exact_source_landing_is_declared_only_gate():
    owner = {
        "gid": TASK,
        "notes": "",
        "completed": False,
        "dependents": [{"gid": DEPENDENT}],
    }
    dependent = {
        "gid": DEPENDENT,
        "notes": f"<!-- dish-source-dependency:v1 upstream={TASK} only_gate=true -->",
        "completed": False,
        "dependencies": [{"gid": TASK}, {"gid": "1217443403986572"}],
    }
    asana = FakeAsana(owner, dependent)
    result = reconcile(asana)
    assert result.dependents_advanced == (DEPENDENT,)
    assert asana.tasks[DEPENDENT]["completed"] is False
    assert [x["gid"] for x in asana.tasks[DEPENDENT]["dependencies"]] == ["1217443403986572"]


def test_review_post_merge_gate_keeps_source_task_open_without_rewriting_notes():
    notes = "<!-- dish-source-work:v1 final_outstanding_gate=true -->"
    asana = FakeAsana({"gid": TASK, "notes": notes, "completed": False, "dependents": []})
    lifecycle = merged_lifecycle()
    lifecycle.post_merge_gates = ["task 1217484567901049 — dual-stack TEST qualification before PROD"]
    result = reconcile_after_merge(
        asana=asana,
        lifecycle=lifecycle,
        repository="marcogallotta/ai-tools",
        merge_sha=MERGE_SHA,
    )
    assert result.completed is False
    assert result.residual_gates == (
        "post-merge:task 1217484567901049 — dual-stack TEST qualification before PROD",
    )
    assert asana.field_updates == []
    assert asana.tasks[TASK]["notes"] == notes
