import io
import json
import os
import subprocess
from pathlib import Path

import pytest


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ai-tools"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "agent/test-reground")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", "https://github.com/marcogallotta/ai-tools.git")

    (repo / "dish/docs/agents").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "CLAUDE.md").write_text("ROOT CURRENT INSTRUCTIONS\n", encoding="utf-8")
    (repo / "dish/docs/agents/index.md").write_text(
        "| Role / common names | Standing contract |\n"
        "|---|---|\n"
        "| Workflow specialist | [`workflow.md`](workflow.md) |\n",
        encoding="utf-8",
    )
    (repo / "dish/docs/agents/workflow.md").write_text(
        "# Workflow specialist\n\n"
        "- Asana project `Dish — Workflow` (`1217381674871544`) is the live coordination authority for Workflow work;\n"
        "- STANDING OBLIGATION RESTORED AFTER COMPACTION.\n",
        encoding="utf-8",
    )
    asana = repo / "tools/asana"
    asana.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "path = sys.argv[-1]\n"
        "if path.startswith('/projects/'):\n"
        "    value = {'gid':'1217381674871544','name':'Dish — Workflow','modified_at':'2026-08-14T00:00:00Z','archived':False}\n"
        "else:\n"
        "    value = {'gid':'1234567890','name':'Owning task','completed':False,'modified_at':'2026-08-14T00:00:00Z','permalink_url':'https://app.asana.com/0/0/1234567890','memberships':[{'project':{'gid':'1217381674871544','name':'Dish — Workflow'}}]}\n"
        "print(json.dumps(value))\n",
        encoding="utf-8",
    )
    asana.chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _install_fake_gh(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps([{'number':77,'url':'https://github.com/marcogallotta/ai-tools/pull/77','state':'OPEN','isDraft':False,'headRefOid':'0123456789abcdef0123456789abcdef01234567','reviewDecision':'CHANGES_REQUESTED','mergeStateStatus':'BLOCKED','mergedAt':None,'closedAt':None}]))\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _write_identity(state_root: Path, repo: Path, agent_id: str = "session-1"):
    path = state_root / "agents" / f"{agent_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "role": "workflow",
                "assigned_at": "2026-08-14T00:00:00Z",
                "workspace": str(repo),
                "owning_task_gid": "1234567890",
                "owning_project_gid": "1217381674871544",
                "active_worktree": {
                    "task_gid": "1234567890",
                    "worktree": str(repo),
                    "branch": "agent/test-reground",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _session_payload(repo: Path, transcript: Path | None = None):
    payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "session_id": "session-1",
        "cwd": str(repo),
    }
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return payload


def test_compaction_reloads_role_asana_and_pr_from_durable_state(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)

    # The compacted hook input deliberately contains none of the standing role obligation,
    # task state, or PR state. The hook must recover all of them mechanically.
    result = agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "ROOT CURRENT INSTRUCTIONS" in context
    assert "STANDING OBLIGATION RESTORED AFTER COMPACTION" in context
    assert "Owning task" in context
    assert "Dish — Workflow" in context
    assert "1217381674871544" in context
    assert "pull/77" in context
    assert "CHANGES_REQUESTED" in context
    assert "UNKNOWN" in context

    marker = json.loads(agent_reground.marker_path("session-1").read_text())
    assert marker["status"] == "ready"
    assert marker["agent_id"] == "session-1"
    assert marker["resolved_role"] == "workflow"
    assert marker["role_contract"]["path"] == "dish/docs/agents/workflow.md"
    assert len(marker["role_contract"]["sha256"]) == 64
    assert marker["role_contract"]["version"] != "UNCOMMITTED_OR_UNTRACKED"
    assert marker["owning_task_gid"] == "1234567890"
    assert marker["owning_project_gid"] == "1217381674871544"
    assert marker["git"]["branch"] == "agent/test-reground"
    assert marker["pr"]["number"] == 77

def test_compaction_uses_shared_asana_launcher_when_worktree_launcher_is_unavailable(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)

    shared_asana = tmp_path / "shared-asana"
    shared_asana.write_text((repo / "tools/asana").read_text(encoding="utf-8"), encoding="utf-8")
    shared_asana.chmod(0o755)
    (repo / "tools/asana").write_text(
        "#!/usr/bin/env python3\nraise SystemExit('worktree virtualenv is unavailable')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_reground, "ASANA_TOOL", shared_asana)

    result = agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    assert "Owning task" in result["hookSpecificOutput"]["additionalContext"]


def test_live_asana_role_requires_owning_task_pointer(agent_reground, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    identity_path = state_root / "agents/session-1.json"
    identity = json.loads(identity_path.read_text())
    identity.pop("owning_task_gid")
    identity["active_worktree"].pop("task_gid")
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    try:
        agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")
    except agent_reground.RegroundError as exc:
        assert "no owning_task_gid" in str(exc)
    else:
        raise AssertionError("expected live Asana role to fail closed without owning task")


def test_history_is_unknown_without_evidence_and_transcript_backed_with_match(
    agent_reground, tmp_path, monkeypatch, capsys
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)

    agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")
    assert agent_reground.history_check("session-1", "read workflow.md") == 0
    output = capsys.readouterr().out
    assert output.startswith("UNKNOWN:")
    assert "non-occurrence" in output

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"message":"I read workflow.md before compaction"}\n', encoding="utf-8")
    agent_reground.perform_reground(_session_payload(repo, transcript), "session-1", "claude")
    assert agent_reground.history_check("session-1", "read workflow.md") == 0
    output = capsys.readouterr().out
    assert output.startswith("TRANSCRIPT_MATCH:")

    assert agent_reground.history_check("session-1", "definitely absent literal") == 0
    output = capsys.readouterr().out
    assert output.startswith("UNKNOWN:")
    assert "does not prove" in output
def test_failed_sessionstart_retains_transcript_context(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"message":"before compaction"}\n', encoding="utf-8")

    def failed_recover_asana(*_args, **_kwargs):
        raise agent_reground.RegroundError("temporary failure")

    monkeypatch.setattr(agent_reground, "recover_asana", failed_recover_asana)
    with pytest.raises(agent_reground.RegroundError):
        agent_reground.perform_reground(
            _session_payload(repo, transcript), "session-1", "claude"
        )
    pending = json.loads(agent_reground.boundary_path("session-1").read_text())
    assert pending["transcript_path"] == str(transcript)

    assert pending["status"] == "pending"
    assert pending["last_error"] == "temporary failure"
    assert not agent_reground.marker_path("session-1").exists()

def test_host_configs_wire_compact_reground_without_global_pretool_barrier(hooks_dir):
    codex = json.loads((hooks_dir.parent / "codex/hooks.json").read_text())
    session = codex["hooks"]["SessionStart"]
    assert any(
        entry.get("matcher") == "^compact$"
        and entry["hooks"][0]["command"] == "/home/marco/.local/bin/agent-reground"
        for entry in session
    )
    pretool = codex["hooks"]["PreToolUse"]
    assert not any(entry["hooks"][0]["command"] == "/home/marco/.local/bin/agent-reground" for entry in pretool)
    assert any(
        entry.get("matcher") == "^Bash$"
        and entry["hooks"][0]["command"] == "/home/marco/.local/bin/codex-protected-checkout"
        for entry in pretool
    )

    claude = json.loads((hooks_dir.parent / ".claude/settings.json").read_text())
    assert claude["hooks"]["SessionStart"][0]["matcher"] == "compact"
    assert "$CLAUDE_PROJECT_DIR" in claude["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "PreToolUse" not in claude["hooks"]


def test_stale_pretool_invocation_is_non_blocking(agent_reground, monkeypatch, capsys):
    payload = {"hook_event_name": "PreToolUse", "session_id": "session-1"}
    monkeypatch.setattr(agent_reground.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert agent_reground.main([]) == 0
    assert capsys.readouterr().out == ""


def test_compaction_hook_is_silent_outside_ai_tools(agent_reground, monkeypatch, capsys):
    payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "session_id": "session-1",
        "cwd": "/tmp/unrelated",
    }
    monkeypatch.setattr(agent_reground.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert agent_reground.main([]) == 0
    assert capsys.readouterr().out == ""


def test_unidentified_codex_session_never_synthesizes_development_workflow_identity(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    monkeypatch.setenv("CODEX_THREAD_ID", "fresh-codex-thread")
    payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "session_id": "payload-session",
        "cwd": str(repo),
    }

    with pytest.raises(agent_reground.RegroundError, match="per-agent identity is missing"):
        agent_reground.perform_reground(payload, "fresh-codex-thread", "codex")

    identity = state_root / "agents/fresh-codex-thread.json"
    assert not identity.exists()
    boundary = json.loads(agent_reground.boundary_path("fresh-codex-thread").read_text())
    assert boundary["status"] == "pending"
    assert "per-agent identity is missing" in boundary["last_error"]


def test_hook_certification_identity_override_requires_explicit_isolated_state(
    agent_reground, tmp_path, monkeypatch
):
    monkeypatch.setenv("DISH_HOOK_CERT_AGENT_ID", "cert-session")
    monkeypatch.setenv("DISH_HOOK_CERT_HOST", "codex")
    monkeypatch.setenv("DISH_HOOK_CERTIFICATION", "1")
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(tmp_path / "ordinary-state"))

    with pytest.raises(agent_reground.RegroundError, match="hook-certification directory"):
        agent_reground.resolve_agent_id({})

    state_root = tmp_path / "hook-certification" / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    assert agent_reground.resolve_agent_id({}) == ("cert-session", "codex")
