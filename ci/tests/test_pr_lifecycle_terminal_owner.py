from __future__ import annotations

from copy import deepcopy

import test_pr_lifecycle as base
import test_pr_lifecycle_terminal as terminal_base
import pr_lifecycle_terminal as terminal

p = base.pr_lifecycle
HEAD = base.HEAD
OWNER = terminal_base.TASK
RELATED = "1217457973835810"


class MultiAsana:
    def __init__(self, owner: dict, related: dict | None = None):
        self.tasks = {str(owner["gid"]): deepcopy(owner)}
        if related is not None:
            self.tasks[str(related["gid"])] = deepcopy(related)

    def get_task(self, gid: str):
        assert gid in self.tasks
        return deepcopy(self.tasks[gid])


def engine(gh, owner: dict, related: dict | None = None):
    return p.LifecycleEngine(gh, asana=MultiAsana(owner, related), now=lambda: base.NOW)


def active_owner():
    return {
        "gid": OWNER,
        "name": "Active canonical owner",
        "completed": False,
        "notes": "Implementation remains active; PR #31 is not terminal.",
    }


def related_terminal():
    return {
        "gid": RELATED,
        "name": "SUPERSEDED — related task",
        "completed": True,
        "notes": "Do not revive PR #31.",
    }


def test_owner_plus_related_reference_cleans_only_canonical_owner():
    gh = terminal_base.TerminalGitHub(
        base.pr(body=f"Asana: `{OWNER}` — canonical owner\nRelated task `{RELATED}` is subsumed")
    )
    cleaner = terminal_base.Cleaner()
    result = engine(gh, terminal_base.superseded_task(), related_terminal()).dispatch(
        include_closed=True, terminal_cleaner=cleaner
    )

    assert p.owning_task_identity_from_references(result[0].task_ids)[0] == OWNER
    assert set(result[0].task_ids) == {OWNER, RELATED}
    assert gh.pr["state"] == "closed"
    assert cleaner.calls == [(31, "agent/test", HEAD, "superseded", (OWNER, RELATED))]


def test_terminal_related_task_cannot_authorize_closing_owner_pr():
    gh = terminal_base.TerminalGitHub(base.pr(body=f"Owning task: {OWNER}\nRelated task: {RELATED}"))
    cleaner = terminal_base.Cleaner()
    result = engine(gh, active_owner(), related_terminal()).dispatch(
        include_closed=True, terminal_cleaner=cleaner
    )

    assert result[0].state == p.LifecycleState.REVIEW_READY
    assert p.owning_task_identity_from_references(result[0].task_ids)[0] == OWNER
    assert set(result[0].task_ids) == {OWNER, RELATED}
    assert gh.closed_calls == 0
    assert cleaner.calls == []


def test_missing_explicit_owner_refuses_terminal_mutation():
    gh = terminal_base.TerminalGitHub(base.pr(body=f"Related task: {OWNER}", state="closed"))
    cleaner = p.TerminalCleanupDispatcher("agent-worktree", repo_path="/repo")
    current = engine(gh, terminal_base.superseded_task()).inspect(gh.pr)

    assert p.owning_task_identity_from_references(current.task_ids)[0] is None
    assert p.owning_task_identity_from_references(current.task_ids)[1] == "explicit owning Asana task is missing"
    try:
        cleaner.dispatch(current, "closed")
    except p.LifecycleError as exc:
        assert "explicit owning Asana task is missing" in str(exc)
    else:
        raise AssertionError("terminal cleanup unexpectedly accepted a missing owner")


def test_conflicting_explicit_owner_declarations_refuse_terminal_mutation():
    gh = terminal_base.TerminalGitHub(
        base.pr(
            body=(
                f"<!-- dish-owning-task:v1 task={OWNER} -->\n"
                f"Owning task: {RELATED}"
            ),
            state="closed",
        )
    )
    current = engine(gh, terminal_base.superseded_task(), related_terminal()).inspect(gh.pr)

    assert p.owning_task_identity_from_references(current.task_ids)[0] is None
    assert "multiple conflicting explicit owning-task declarations" in (p.owning_task_identity_from_references(current.task_ids)[1] or "")
    assert terminal.asana_terminal_decision(current) is None


def test_cleanup_command_binds_owner_not_related_reference(monkeypatch):
    gh = terminal_base.TerminalGitHub(
        base.pr(body=f"Asana: {OWNER}\nRelated task: {RELATED}", state="closed")
    )
    current = engine(gh, terminal_base.superseded_task(), related_terminal()).inspect(gh.pr)
    captured = {}

    class Completed:
        returncode = 0
        stdout = '{"ok": true, "remote_branch_removed": true}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr("pr_lifecycle_terminal.subprocess.run", fake_run)
    p.TerminalCleanupDispatcher("agent-worktree", repo_path="/repo").dispatch(current, "closed")

    command = captured["command"]
    assert command[command.index("--task") + 1] == OWNER
    assert RELATED not in command
