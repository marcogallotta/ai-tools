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

    assert agent_reground.validate_barrier(
        {"hook_event_name": "PreToolUse", "session_id": "session-1", "cwd": str(repo)},
        "session-1",
    ) is None


def test_pretool_fails_closed_for_missing_or_stale_marker(agent_reground, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    agent_reground.marker_path("session-1").unlink()
    reason = agent_reground.validate_barrier(
        {"hook_event_name": "PreToolUse", "session_id": "session-1", "cwd": str(repo)},
        "session-1",
    )
    assert "marker" in reason

    agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")
    contract = repo / "dish/docs/agents/workflow.md"
    contract.write_text(contract.read_text() + "changed role authority\n", encoding="utf-8")
    reason = agent_reground.validate_barrier(
        {"hook_event_name": "PreToolUse", "session_id": "session-1", "cwd": str(repo)},
        "session-1",
    )
    assert "role contract changed" in reason


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



def _pretool_payload(repo: Path):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "cwd": str(repo),
    }


def test_pretool_self_recovers_after_transient_sessionstart_failure(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)

    real_recover_asana = agent_reground.recover_asana
    calls = {"count": 0}

    def flaky_recover_asana(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise agent_reground.RegroundError("transient Asana read failure")
        return real_recover_asana(*args, **kwargs)

    monkeypatch.setattr(agent_reground, "recover_asana", flaky_recover_asana)
    with pytest.raises(agent_reground.RegroundError, match="transient Asana read failure"):
        agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    pending = json.loads(agent_reground.boundary_path("session-1").read_text())
    assert pending["status"] == "pending"
    assert pending["last_error"] == "transient Asana read failure"
    assert pending["last_failure_at"]
    assert pending["reground_attempt_count"] == 1
    assert not agent_reground.marker_path("session-1").exists()

    decision = agent_reground.recover_pretool_barrier(
        _pretool_payload(repo), "session-1", "claude"
    )
    assert decision is None
    assert calls["count"] == 2

    boundary = json.loads(agent_reground.boundary_path("session-1").read_text())
    marker = json.loads(agent_reground.marker_path("session-1").read_text())
    assert boundary["status"] == "ready"
    assert boundary["reground_attempt_count"] == 2
    assert boundary["last_attempt_event"] == "PreToolUse"
    assert boundary["last_error"] == "transient Asana read failure"
    assert marker["status"] == "ready"
    assert agent_reground.validate_barrier(_pretool_payload(repo), "session-1") is None


def test_pretool_persistent_failure_retries_once_then_denies_concrete_cause(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)

    calls = {"count": 0}

    def failed_recover_asana(*_args, **_kwargs):
        calls["count"] += 1
        raise agent_reground.RegroundError("Asana API remains unavailable")

    monkeypatch.setattr(agent_reground, "recover_asana", failed_recover_asana)
    with pytest.raises(agent_reground.RegroundError, match="Asana API remains unavailable"):
        agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    before = calls["count"]
    decision = agent_reground.recover_pretool_barrier(
        _pretool_payload(repo), "session-1", "claude"
    )
    assert calls["count"] == before + 1
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Asana API remains unavailable" in reason
    assert "attempted once" in reason
    assert not agent_reground.marker_path("session-1").exists()
    boundary = json.loads(agent_reground.boundary_path("session-1").read_text())
    assert boundary["status"] == "pending"
    assert boundary["reground_attempt_count"] == 2


def test_pretool_invalid_marker_self_heals(agent_reground, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    agent_reground.marker_path("session-1").write_text("{not-json\n", encoding="utf-8")
    assert "cannot read re-grounding marker" in agent_reground.validate_barrier(
        _pretool_payload(repo), "session-1"
    )
    assert agent_reground.recover_pretool_barrier(
        _pretool_payload(repo), "session-1", "claude"
    ) is None
    assert agent_reground.validate_barrier(_pretool_payload(repo), "session-1") is None


def test_pretool_role_authority_drift_self_heals(agent_reground, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    contract = repo / "dish/docs/agents/workflow.md"
    contract.write_text(contract.read_text() + "CURRENT AUTHORITY CHANGE\n", encoding="utf-8")
    assert "role contract changed" in agent_reground.validate_barrier(
        _pretool_payload(repo), "session-1"
    )

    assert agent_reground.recover_pretool_barrier(
        _pretool_payload(repo), "session-1", "claude"
    ) is None
    marker = json.loads(agent_reground.marker_path("session-1").read_text())
    assert marker["role_contract"]["sha256"] == agent_reground._sha256_text(contract.read_text())
    assert agent_reground.validate_barrier(_pretool_payload(repo), "session-1") is None


def test_pretool_real_branch_identity_conflict_remains_denied(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    agent_reground.perform_reground(_session_payload(repo), "session-1", "claude")

    identity_path = state_root / "agents/session-1.json"
    identity = json.loads(identity_path.read_text())
    identity["active_worktree"]["branch"] = "agent/different-branch"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    decision = agent_reground.recover_pretool_barrier(
        _pretool_payload(repo), "session-1", "claude"
    )
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "durable identity branch 'agent/different-branch'" in reason
    boundary = json.loads(agent_reground.boundary_path("session-1").read_text())
    assert boundary["status"] == "pending"


def test_failed_sessionstart_recovery_retains_transcript_context(
    agent_reground, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"message":"before compaction"}\n', encoding="utf-8")

    real_recover_asana = agent_reground.recover_asana
    calls = {"count": 0}

    def flaky_recover_asana(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise agent_reground.RegroundError("temporary failure")
        return real_recover_asana(*args, **kwargs)

    monkeypatch.setattr(agent_reground, "recover_asana", flaky_recover_asana)
    with pytest.raises(agent_reground.RegroundError):
        agent_reground.perform_reground(
            _session_payload(repo, transcript), "session-1", "claude"
        )
    pending = json.loads(agent_reground.boundary_path("session-1").read_text())
    assert pending["transcript_path"] == str(transcript)

    assert agent_reground.recover_pretool_barrier(
        _pretool_payload(repo), "session-1", "claude"
    ) is None
    marker = json.loads(agent_reground.marker_path("session-1").read_text())
    assert marker["transcript"]["path"] == str(transcript)
    assert marker["transcript"]["accessible"] is True

    # Missing transcript evidence stays UNKNOWN rather than being invented during recovery.
    other_state = tmp_path / "other-state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(other_state))
    _write_identity(other_state, repo, agent_id="session-2")
    payload = {
        "hook_event_name": "SessionStart",
        "source": "compact",
        "session_id": "session-2",
        "cwd": str(repo),
    }
    agent_reground.perform_reground(payload, "session-2", "claude")
    marker2 = json.loads(agent_reground.marker_path("session-2").read_text())
    assert marker2["transcript"]["path"] is None
    assert marker2["transcript"]["accessible"] is False

def test_host_configs_wire_compact_and_pretool_barriers(hooks_dir):
    codex = json.loads((hooks_dir.parent / "codex/hooks.json").read_text())
    session = codex["hooks"]["SessionStart"]
    assert any(
        entry.get("matcher") == "^compact$"
        and entry["hooks"][0]["command"] == "/home/marco/.local/bin/agent-reground"
        for entry in session
    )
    pretool = codex["hooks"]["PreToolUse"]
    assert any(entry["hooks"][0]["command"] == "/home/marco/.local/bin/agent-reground" for entry in pretool)
    assert any(
        entry.get("matcher") == "^Bash$"
        and entry["hooks"][0]["command"] == "/home/marco/.local/bin/codex-protected-checkout"
        for entry in pretool
    )

    codex_session_timeout = codex["hooks"]["SessionStart"][0]["hooks"][0]["timeout"]
    codex_reground_pretool = next(
        entry["hooks"][0]
        for entry in pretool
        if entry["hooks"][0]["command"] == "/home/marco/.local/bin/agent-reground"
    )
    assert codex_reground_pretool["timeout"] >= codex_session_timeout

    claude = json.loads((hooks_dir.parent / ".claude/settings.json").read_text())
    assert claude["hooks"]["SessionStart"][0]["matcher"] == "compact"
    assert "$CLAUDE_PROJECT_DIR" in claude["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "PreToolUse" in claude["hooks"]
    claude_session_timeout = claude["hooks"]["SessionStart"][0]["hooks"][0]["timeout"]
    claude_reground_pretool = claude["hooks"]["PreToolUse"][0]["hooks"][0]
    assert claude_reground_pretool["timeout"] >= claude_session_timeout


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
