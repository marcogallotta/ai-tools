import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "hooks"


def _load_hook_module(name):
    path = HOOKS_DIR / name
    loader = SourceFileLoader(f"{name}_ci_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def agent_grounding():
    return _load_hook_module("agent-grounding")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


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
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/marcogallotta/ai-tools.git",
    )

    (repo / "dish/docs/agents").mkdir(parents=True)
    (repo / "dish/docs/chatgpt-projects").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "tools").mkdir()
    (repo / "CLAUDE.md").write_text(
        "ROOT CURRENT INSTRUCTIONS\n", encoding="utf-8"
    )
    (repo / "OPERATOR_CONTROL_PLANE.md").write_text(
        "# Shared operator control plane\n\n"
        "All standing roles apply Shared operator interaction. Coordinator and Development Workflow additionally apply the task-specific sections.\n\n"
        "## Shared operator interaction\n\n"
        "SHARED OPERATOR CONTROL PLANE\n\n"
        "## TRUE READY dispatch queue\n\n"
        "COORDINATOR-ONLY QUEUE DETAIL\n",
        encoding="utf-8",
    )
    (repo / "dish/docs/agents/index.md").write_text(
        "All repository-modifying roles inherit [`contributor-base.md`](contributor-base.md).\n\n"
        "All roles also apply the shared [`Dish operator / orchestration control plane`](../../../OPERATOR_CONTROL_PLANE.md) for presentation mechanics. Coordinator and Development Workflow additionally apply its action-specific queue/handoff/decision/triage sections.\n\n"
        "| Role / common names | Standing contract |\n"
        "|---|---|\n"
        "| Coordinator | [`coordinator.md`](coordinator.md) |\n"
        "| Development Workflow specialist | [`development-workflow.md`](development-workflow.md) |\n"
        "| Workflow specialist | [`workflow.md`](workflow.md) |\n",
        encoding="utf-8",
    )
    (repo / "dish/docs/agents/coordinator.md").write_text(
        "# Coordinator\n", encoding="utf-8"
    )
    (repo / "dish/docs/agents/development-workflow.md").write_text(
        "# Development Workflow\n", encoding="utf-8"
    )
    (repo / "dish/docs/agents/workflow.md").write_text(
        "# Workflow specialist\n\n"
        "Asana project `Dish — Workflow` (`1217381674871544`) is the live coordination authority for Workflow work.\n\n"
        "## Action context\n\n"
        "ACTION-SPECIFIC AUTHORITY RESTORED.\n",
        encoding="utf-8",
    )
    (repo / "dish/docs/agents/contributor-base.md").write_text(
        "# Contributor base\n", encoding="utf-8"
    )
    (repo / "dish/docs/chatgpt-projects/source.json").write_text(
        json.dumps(
            {
                "roles": {
                    "coordinator": {
                        "contract": "dish/docs/agents/coordinator.md",
                        "allowed_compositions": [],
                    },
                    "development-workflow": {
                        "contract": "dish/docs/agents/development-workflow.md",
                        "allowed_compositions": [],
                    },
                    "workflow": {
                        "contract": "dish/docs/agents/workflow.md",
                        "allowed_compositions": [],
                        "context_dependencies": {
                            "triggered_reads": {
                                "safe action": [
                                    "dish/docs/agents/workflow.md#Action context"
                                ]
                            }
                        },
                    },
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


def _write_identity(state_root: Path, repo: Path, *, role: str = "workflow"):
    path = state_root / "agents/session-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent_id": "session-1",
                "role": role,
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


def _setup(tmp_path, monkeypatch, *, identity: bool = True):
    repo = _make_repo(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    state_root = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state_root))
    if identity:
        _write_identity(state_root, repo)
    return repo, state_root


def test_true_fresh_session_bootstraps_without_inventing_role_then_upgrades(
    agent_grounding, tmp_path, monkeypatch
):
    repo, state = _setup(tmp_path, monkeypatch, identity=False)

    fresh = agent_grounding._session_ground(
        _session_payload(repo, "startup"),
        "session-1",
        "claude",
        session_source="startup",
    )
    context = fresh["hookSpecificOutput"]["additionalContext"]
    assert "DISH PRE-ROLE SESSION BOOTSTRAP" in context
    assert "No role has been inferred or granted" in context
    assert "ROOT CURRENT INSTRUCTIONS" in context
    assert not agent_grounding.BASE.boundary_path("session-1").exists()

    marker = json.loads(
        agent_grounding.BASE.marker_path("session-1").read_text()
    )
    assert marker["status"] == "pre-role"
    assert marker["resolved_role"] is None
    assert marker["session_grounding"]["phase"] == "pre-role"
    assert marker["session_grounding"]["source"] == "startup"
    assert "transcript" not in marker

    denied = agent_grounding._pretool(
        _pretool_payload(repo), "session-1", "claude"
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "per-agent identity is not yet available" in denied[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]

    _write_identity(state, repo)
    upgraded = agent_grounding._pretool(
        _pretool_payload(repo), "session-1", "claude"
    )
    assert upgraded is not None
    upgraded_context = upgraded["hookSpecificOutput"]["additionalContext"]
    assert "pre-role bootstrap upgraded to full role grounding" in upgraded_context
    assert "SHARED OPERATOR CONTROL PLANE" in upgraded_context

    marker = json.loads(
        agent_grounding.BASE.marker_path("session-1").read_text()
    )
    assert marker["status"] == "ready"
    assert marker["resolved_role"] == "workflow"
    assert marker["session_grounding"]["phase"] == "full-role"
    assert marker["session_grounding"]["source"] == "action-recovery"
    assert marker["last_tool_witness"]["tool_name"] == "Bash"
    assert not agent_grounding.BASE.boundary_path("session-1").exists()


def test_fresh_and_resume_do_not_create_compaction_boundary(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(tmp_path, monkeypatch)

    fresh = agent_grounding._session_ground(
        _session_payload(repo, "startup"),
        "session-1",
        "claude",
        session_source="startup",
    )
    fresh_context = fresh["hookSpecificOutput"]["additionalContext"]
    assert "source=startup" in fresh_context
    assert "ROOT CURRENT INSTRUCTIONS" in fresh_context
    assert "SHARED OPERATOR CONTROL PLANE" in fresh_context
    assert "COORDINATOR-ONLY QUEUE DETAIL" not in fresh_context
    assert "DISH CORE RE-GROUNDING" in fresh_context
    assert "POST-COMPACTION DISH RE-GROUNDING" not in fresh_context
    assert "SELF-HISTORY VERIFICATION RULE" not in fresh_context
    assert not agent_grounding.BASE.boundary_path("session-1").exists()

    marker = json.loads(
        agent_grounding.BASE.marker_path("session-1").read_text()
    )
    assert marker["session_grounding"]["source"] == "startup"
    assert "transcript" not in marker

    receipt = json.loads(
        agent_grounding._session_receipt_path("session-1").read_text()
    )
    first_generation = receipt["grounding_generation"]
    assert receipt["session_source"] == "startup"
    assert receipt["role"] == "workflow"
    assert {record["locator"] for record in receipt["context_records"]} == {
        "dish/docs/agents/workflow.md",
        "OPERATOR_CONTROL_PLANE.md#Shared operator interaction",
    }
    assert all(
        len(record["content_sha256"]) == 64
        for record in receipt["context_records"]
    )

    resumed = agent_grounding._session_ground(
        _session_payload(repo, "resume"),
        "session-1",
        "claude",
        session_source="resume",
    )
    resumed_context = resumed["hookSpecificOutput"]["additionalContext"]
    assert "source=resume" in resumed_context
    assert "POST-COMPACTION DISH RE-GROUNDING" not in resumed_context
    assert "SELF-HISTORY VERIFICATION RULE" not in resumed_context
    assert not agent_grounding.BASE.boundary_path("session-1").exists()
    marker = json.loads(
        agent_grounding.BASE.marker_path("session-1").read_text()
    )
    assert marker["session_grounding"]["source"] == "resume"
    assert "transcript" not in marker
    receipt = json.loads(
        agent_grounding._session_receipt_path("session-1").read_text()
    )
    assert receipt["session_source"] == "resume"
    assert receipt["grounding_generation"] != first_generation


def test_true_compaction_retains_boundary_and_history_semantics(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(tmp_path, monkeypatch)

    compact = agent_grounding._session_ground(
        _session_payload(repo, "compact"),
        "session-1",
        "claude",
        session_source="compact",
    )
    compact_context = compact["hookSpecificOutput"]["additionalContext"]
    assert "POST-COMPACTION DISH RE-GROUNDING" in compact_context
    assert "SELF-HISTORY VERIFICATION RULE" in compact_context

    boundary = json.loads(
        agent_grounding.BASE.boundary_path("session-1").read_text()
    )
    assert boundary["source"] == "compact"
    assert boundary["status"] == "ready"
    assert "compaction_generation" in boundary
    marker = json.loads(
        agent_grounding.BASE.marker_path("session-1").read_text()
    )
    assert marker["session_grounding"]["source"] == "compact"
    assert marker["session_grounding"]["phase"] == "full-role"
    assert marker["transcript"]["history_before_boundary"] == (
        "VERIFY_TRANSCRIPT_OR_UNKNOWN"
    )


def test_missing_session_witness_self_heals_without_compaction_boundary(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(tmp_path, monkeypatch)
    assert not agent_grounding.BASE.marker_path("session-1").exists()
    assert not agent_grounding.BASE.boundary_path("session-1").exists()

    decision = agent_grounding._pretool(
        _pretool_payload(repo), "session-1", "claude"
    )
    assert decision is not None
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "missing session witness recovered before tool use" in context
    marker = json.loads(
        agent_grounding.BASE.marker_path("session-1").read_text()
    )
    assert marker["status"] == "ready"
    assert marker["session_grounding"]["source"] == "action-recovery"
    assert marker["last_tool_witness"]["tool_name"] == "Bash"
    assert (
        marker["last_tool_witness"]["grounding_generation"]
        == marker["grounding_generation"]
    )
    assert not agent_grounding.BASE.boundary_path("session-1").exists()


def test_existing_but_unknown_role_fails_closed_instead_of_pre_role_fallback(
    agent_grounding, tmp_path, monkeypatch
):
    repo, state = _setup(tmp_path, monkeypatch)
    _write_identity(state, repo, role="not-a-role")
    with pytest.raises(agent_grounding.BASE.RegroundError):
        agent_grounding._session_ground(
            _session_payload(repo, "startup"),
            "session-1",
            "claude",
            session_source="startup",
        )
    assert not agent_grounding.BASE.boundary_path("session-1").exists()


def test_shared_context_drift_reloads_before_tool_use(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(tmp_path, monkeypatch)
    agent_grounding._session_ground(
        _session_payload(repo, "startup"),
        "session-1",
        "claude",
        session_source="startup",
    )
    operator = repo / "OPERATOR_CONTROL_PLANE.md"
    operator.write_text(
        operator.read_text(encoding="utf-8").replace(
            "SHARED OPERATOR CONTROL PLANE",
            "SHARED OPERATOR CONTROL PLANE\nCURRENT POLICY CHANGE",
        ),
        encoding="utf-8",
    )

    decision = agent_grounding._pretool(
        _pretool_payload(repo), "session-1", "claude"
    )
    assert decision is not None
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "required shared/inherited context changed after grounding" in context
    assert "CURRENT POLICY CHANGE" in context
    assert not agent_grounding.BASE.boundary_path("session-1").exists()


def test_declared_action_trigger_loads_bounded_context_and_records_witness(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(tmp_path, monkeypatch)
    agent_grounding._session_ground(
        _session_payload(repo, "startup"),
        "session-1",
        "claude",
        session_source="startup",
    )

    decision = agent_grounding._pretool(
        _pretool_payload(repo, dish_action_trigger="safe action"),
        "session-1",
        "claude",
    )
    assert decision is not None
    context = decision["hookSpecificOutput"]["additionalContext"]
    assert "ACTION-SPECIFIC AUTHORITY RESTORED" in context
    assert "# Workflow specialist" not in context

    receipt = json.loads(
        agent_grounding._action_receipt_path("session-1").read_text()
    )
    assert receipt["trigger"] == "safe action"
    assert receipt["tool_name"] == "Bash"
    assert receipt["context_records"][0]["locator"] == (
        "dish/docs/agents/workflow.md#Action context"
    )
    assert len(receipt["context_records"][0]["content_sha256"]) == 64


def test_unknown_action_trigger_fails_closed(
    agent_grounding, tmp_path, monkeypatch
):
    repo, _state = _setup(tmp_path, monkeypatch)
    agent_grounding._session_ground(
        _session_payload(repo, "startup"),
        "session-1",
        "claude",
        session_source="startup",
    )
    decision = agent_grounding._pretool(
        _pretool_payload(repo, dish_action_trigger="not declared"),
        "session-1",
        "claude",
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "action-specific grounding failed" in decision[
        "hookSpecificOutput"
    ]["permissionDecisionReason"]
