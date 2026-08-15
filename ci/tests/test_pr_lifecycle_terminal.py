from __future__ import annotations

from copy import deepcopy

import test_pr_lifecycle as base

p = base.pr_lifecycle
HEAD = base.HEAD
TASK = "1217449727834334"


class FakeAsana:
    def __init__(self, task: dict):
        self.task = deepcopy(task)

    def get_task(self, gid: str):
        assert gid == TASK
        return deepcopy(self.task)


class TerminalGitHub(base.FakeGitHub):
    def __init__(self, candidate=None, *, protected=False):
        super().__init__(candidate)
        self.protected = protected
        self.closed_calls = 0

    def close_pr(self, number):
        assert number == self.pr["number"]
        self.events.append(("close", self.pr["head"]["sha"]))
        self.closed_calls += 1
        self.pr["state"] = "closed"
        self.pr["merged"] = False
        self.pr["merged_at"] = None
        return deepcopy(self.pr)

    def get_branch(self, branch):
        assert branch == self.pr["head"]["ref"]
        return {"name": branch, "protected": self.protected, "commit": {"sha": self.pr["head"]["sha"]}}


class Cleaner:
    command = "fixture"

    def __init__(self):
        self.calls = []

    def dispatch(self, lifecycle, disposition):
        self.calls.append((lifecycle.number, lifecycle.branch, lifecycle.head, disposition, tuple(lifecycle.task_ids)))
        return {"ok": True, "remote_branch_removed": True, "local_state_present": True}


def lifecycle(gh, task):
    return p.LifecycleEngine(gh, asana=FakeAsana(task), now=lambda: base.NOW)


def superseded_task():
    return {
        "gid": TASK,
        "name": "SUPERSEDED — Cheap preflight folded into selector-driven certification",
        "completed": True,
        "completed_at": base.NOW.isoformat(),
        "notes": (
            "SUPERSEDED / CLOSED\n"
            "Do not revive/land PR #31 as-is.\n"
            "Current authority is P-CRITICAL task 1217457973835810."
        ),
        "permalink_url": f"https://app.asana.com/0/0/{TASK}",
    }


def test_explicit_supersession_closes_unmerged_then_cleans_exact_lineage_idempotently():
    gh = TerminalGitHub(base.pr(body=f"Owning task: {TASK}"))
    cleaner = Cleaner()
    engine = lifecycle(gh, superseded_task())

    first = engine.dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert first[0].state == p.LifecycleState.CLOSED
    assert gh.pr["state"] == "closed" and gh.pr["merged"] is False
    assert gh.closed_calls == 1
    assert cleaner.calls == [(31, "agent/test", HEAD, "superseded", (TASK,))]
    bodies = [item["body"] for item in gh.comments]
    assert any("dish-terminal-disposition:v1 disposition=superseded" in body for body in bodies)
    assert any("replacement_task=1217457973835810" in body for body in bodies)
    assert any("dish-terminal-cleanup:v1 disposition=superseded" in body for body in bodies)

    second = engine.dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert second[0].state == p.LifecycleState.CLOSED
    assert gh.closed_calls == 1
    assert len(cleaner.calls) == 1


def test_parked_or_temporarily_blocked_pr_is_not_terminal_authority():
    gh = TerminalGitHub(base.pr(body=f"Owning task: {TASK}"))
    cleaner = Cleaner()
    task = {
        "gid": TASK,
        "name": "Cheap preflight",
        "completed": False,
        "notes": "PARKED / DEFERRED — PR #31 remains useful; no Integration action expected yet.",
    }
    result = lifecycle(gh, task).dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert result[0].state == p.LifecycleState.REVIEW_READY
    assert gh.closed_calls == 0
    assert cleaner.calls == []


def test_active_umbrella_can_authoritatively_supersede_named_pr():
    gh = TerminalGitHub(base.pr(body=f"Owning task: {TASK}"))
    cleaner = Cleaner()
    task = {
        "gid": TASK,
        "name": "P-CRITICAL umbrella",
        "completed": False,
        "notes": "Superseded lineages\nDo not touch/revive PR #31 topology unless Marco explicitly reauthorizes it.",
    }
    lifecycle(gh, task).dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert gh.closed_calls == 1
    assert cleaner.calls[0][3] == "superseded"


def test_protected_terminal_agent_branch_is_never_cleaned():
    candidate = base.pr(body=f"Owning task: {TASK}", state="closed")
    gh = TerminalGitHub(candidate, protected=True)
    cleaner = Cleaner()
    result = lifecycle(gh, superseded_task()).dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert result[0].state == p.LifecycleState.CLOSED
    assert cleaner.calls == []
    assert "protected" in (result[0].residual_reason or "")


def test_non_agent_terminal_branch_is_never_cleaned():
    candidate = base.pr(body=f"Owning task: {TASK}", state="closed")
    candidate["head"]["ref"] = "main"
    gh = TerminalGitHub(candidate)
    cleaner = Cleaner()
    result = lifecycle(gh, superseded_task()).dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert cleaner.calls == []
    assert "not an agent/* branch" in (result[0].residual_reason or "")


def test_already_merged_lineage_is_cleaned_on_terminal_scan():
    candidate = base.pr(body=f"Owning task: {TASK}", state="closed", merged=True)
    gh = TerminalGitHub(candidate)
    gh.pr["merged"] = True
    gh.pr["merged_at"] = base.NOW.isoformat()
    cleaner = Cleaner()
    result = lifecycle(gh, superseded_task()).dispatch(include_closed=True, terminal_cleaner=cleaner)
    assert result[0].state == p.LifecycleState.MERGED
    assert cleaner.calls == [(31, "agent/test", HEAD, "merged", (TASK,))]
