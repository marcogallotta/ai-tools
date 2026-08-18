import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _install_fake_gh(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps([{'number':88,'url':'https://github.com/marcogallotta/ai-tools/pull/88','state':'OPEN','isDraft':True,'headRefOid':'0123456789abcdef0123456789abcdef01234567','reviewDecision':'','mergeStateStatus':'UNKNOWN','mergedAt':None,'closedAt':None}]))\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ai-tools"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "agent/test-grounding")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", "https://github.com/marcogallotta/ai-tools.git")

    (repo / "dish/docs/agents").mkdir(parents=True)
    (repo / "dish/docs/chatgpt-projects").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "tools").mkdir()
    (repo / "CLAUDE.md").write_text("ROOT CURRENT INSTRUCTIONS\n", encoding="utf-8")
    (repo / "OPERATOR_CONTROL_PLANE.md").write_text("SHARED OPERATOR CONTROL PLANE\n", encoding="utf-8")
    (repo / "dish/docs/agents/index.md").write_text(
        "All roles also apply the shared [control plane](../../../OPERATOR_CONTROL_PLANE.md).\n\n"
        "| Role / common names | Standing contract |\n"
        "|---|---|\n"
        "| Workflow specialist | [`workflow.md`](workflow.md) |\n",
        encoding="utf-8",
    )
    (repo / "dish/docs/agents/workflow.md").write_text(
        "# Workflow specialist\n\n"
        "Asana project `Dish — Workflow` (`1217381674871544`) is the live coordination authority for Workflow work.\n\n"
        "## Action context\n\n"
        "ACTION-SPECIFIC AUTHORITY RESTORED.\n",
        encoding="utf-8",
    )
    (repo / "dish/docs/chatgpt-projects/source.json").write_text(
        json.dumps(
            {
                "roles": {
                    "workflow": {
                        "contract": "dish/docs/agents/workflow.md",
                        "allowed_compositions": [],
                        "context_dependencies": {
                            "triggered_reads": {
                                "safe action": ["dish/docs/agents/workflow.md#Action context"]
                            }
                        },
                    }
                },
                "context_dependencies": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "scripts/agent_context.py").write_text(
        (REPO_ROOT / "scripts/agent_context.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    asana = repo / "tools/asana"
    asana.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "path = sys.argv[-1]\n"
        "if path.startswith('/projects/'):\n"
        "    value = {'gid':'1217381674871544','name':'Dish — Workflow','modified_at':'2026-08-18T00:00:00Z','archived':False}\n"
        "else:\n"
        "    value = {'gid':'1234567890','name':'Owning task','completed':False,'modified_at':'2026-08-18T00:00:00Z','permalink_url':'https://app.asana.com/0/0/1234567890','memberships':[{'project':{'gid':'1217381674871544','name':'Dish — Workflow'}}]}\n"
        "print(json.dumps(value))\n",
        encoding="utf-8",
    )
    asana.chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _write_identity(state_root: Path, repo: Path):
    path = state_root / "agents/session-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "agent_id": "session-1",
                "role": "workflow",
                "assigned_at": "2026-08-18T00:00:00Z",
                "workspace": str(repo),
                "owning_task_gid": "1234567890",
                "owning_project_gid": "1217381674871544",
                "active_worktree": {
                    "task_gid": "1234567890",
                    "worktree": str(repo),
                    "branch": "agent/test-grounding",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _session_payload(repo: Path, source: str):
    return {
        "hook_event_name": "SessionStart",
        "source": source,
        "session_id": "session-1",
        "cwd": str(repo),
    }


def _pretool_payload(repo: Path, **extra):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "cwd": str(repo),
        "tool_name": "Bash",
        **extra,
    }


def _setup(agent_grounding, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    _write_identity(state_root, repo)
    return repo, state_root


def test_fresh_and_resume_sessions_receive_exact_shared_grounding(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(agent_grounding, tmp_path, monkeypatch)

    fresh = agent_grounding._session_ground(
        _session_payload(repo, "startup"), "session-1", "claude", session_source="startup"
    )
    fresh_context = fresh["hookSpecificOutput"]["additionalContext"]
    assert "source=startup" in fresh_context
    assert "ROOT CURRENT INSTRUCTIONS" in fresh_context
    assert "SHARED OPERATOR CONTROL PLANE" in fresh_context

    receipt = json.loads(agent_grounding._session_receipt_path("session-1").read_text())
    first_generation = receipt["grounding_generation"]
    assert receipt["session_source"] == "startup"
    assert receipt["role"] == "workflow"
    assert {record["path"] for record in receipt["context_records"]} == {
        "dish/docs/agents/workflow.md",
        "OPERATOR_CONTROL_PLANE.md",
    }
    assert all(len(record["sha256"]) == 64 for record in receipt["context_records"])

    resumed = agent_grounding._session_ground(
        _session_payload(repo, "resume"), "session-1", "claude", session_source="resume"
    )
    assert "source=resume" in resumed["hookSpecificOutput"]["additionalContext"]
    receipt = json.loads(agent_grounding._session_receipt_path("session-1").read_text())
    assert receipt["session_source"] == "resume"
    assert receipt["grounding_generation"] != first_generation


def test_missing_session_witness_self_heals_before_first_tool(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(agent_grounding, tmp_path, monkeypatch)
    assert not agent_grounding.BASE.marker_path("session-1").exists()
    assert not agent_grounding.BASE.boundary_path("session-1").exists()

    decision = agent_grounding._pretool(_pretool_payload(repo), "session-1", "claude")
    assert decision is not None
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "missing session witness recovered before tool use" in context
    marker = json.loads(agent_grounding.BASE.marker_path("session-1").read_text())
    assert marker["status"] == "ready"
    assert marker["session_grounding"]["source"] == "action-recovery"
    assert marker["last_tool_witness"]["tool_name"] == "Bash"
    assert marker["last_tool_witness"]["grounding_generation"] == marker["grounding_generation"]


def test_shared_context_drift_reloads_before_tool_use(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(agent_grounding, tmp_path, monkeypatch)
    agent_grounding._session_ground(
        _session_payload(repo, "startup"), "session-1", "claude", session_source="startup"
    )
    operator = repo / "OPERATOR_CONTROL_PLANE.md"
    operator.write_text("SHARED OPERATOR CONTROL PLANE\nCURRENT POLICY CHANGE\n", encoding="utf-8")

    decision = agent_grounding._pretool(_pretool_payload(repo), "session-1", "claude")
    assert decision is not None
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "required shared/inherited context changed after grounding" in context
    assert "CURRENT POLICY CHANGE" in context
    marker = json.loads(agent_grounding.BASE.marker_path("session-1").read_text())
    operator_record = next(
        record for record in marker["session_grounding"]["context_records"]
        if record["path"] == "OPERATOR_CONTROL_PLANE.md"
    )
    assert len(operator_record["sha256"]) == 64


def test_declared_action_trigger_loads_bounded_context_and_records_witness(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(agent_grounding, tmp_path, monkeypatch)
    agent_grounding._session_ground(
        _session_payload(repo, "startup"), "session-1", "claude", session_source="startup"
    )

    decision = agent_grounding._pretool(
        _pretool_payload(repo, dish_action_trigger="safe action"), "session-1", "claude"
    )
    assert decision is not None
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "ACTION-SPECIFIC AUTHORITY RESTORED" in context
    assert "# Workflow specialist" not in context

    receipt = json.loads(agent_grounding._action_receipt_path("session-1").read_text())
    assert receipt["trigger"] == "safe action"
    assert receipt["tool_name"] == "Bash"
    assert receipt["context_records"][0]["locator"] == "dish/docs/agents/workflow.md#Action context"
    assert len(receipt["context_records"][0]["content_sha256"]) == 64


def test_unknown_action_trigger_fails_closed(agent_grounding, tmp_path, monkeypatch):
    repo, _state = _setup(agent_grounding, tmp_path, monkeypatch)
    agent_grounding._session_ground(
        _session_payload(repo, "startup"), "session-1", "claude", session_source="startup"
    )
    decision = agent_grounding._pretool(
        _pretool_payload(repo, dish_action_trigger="not declared"), "session-1", "claude"
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "action-specific grounding failed" in decision["hookSpecificOutput"]["permissionDecisionReason"]
