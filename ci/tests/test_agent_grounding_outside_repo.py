import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "hooks/agent-grounding"


def _load_hook():
    loader = SourceFileLoader("agent_grounding_outside_repo_test", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_fresh_codex_outside_repo_binds_installed_hook_checkout(
    tmp_path, monkeypatch
):
    grounding = _load_hook()
    candidate = tmp_path / "candidate-ai-tools"
    outside = tmp_path / "unrelated-repo"
    candidate.mkdir()
    outside.mkdir()

    state = tmp_path / "state"
    monkeypatch.setenv("DISH_AGENT_STATE_ROOT", str(state))
    monkeypatch.setattr(
        grounding,
        "__file__",
        str(candidate / "hooks/agent-grounding"),
    )

    seen = []

    def repo_from_candidate(raw):
        seen.append(raw)
        if raw == str(candidate):
            return candidate
        if raw == str(outside):
            return outside
        return None

    def verify_repository(repo):
        if repo != candidate:
            raise grounding.BASE.RegroundError(
                f"active checkout is not {grounding.BASE.EXPECTED_REPOSITORY}"
            )

    def read_required(repo, relative):
        assert repo == candidate
        return {
            "CLAUDE.md": "ROOT CURRENT INSTRUCTIONS\n",
            "dish/docs/agents/index.md": "| Role | Contract |\n",
        }[relative]

    monkeypatch.setattr(grounding.BASE, "_repo_from_candidate", repo_from_candidate)
    monkeypatch.setattr(grounding.BASE, "verify_repository", verify_repository)
    monkeypatch.setattr(grounding.BASE, "_read_required", read_required)
    monkeypatch.setattr(
        grounding.BASE,
        "_authority_record",
        lambda repo, relative, text: {
            "path": relative,
            "version": "test",
            "sha256": grounding._sha256(text),
        },
    )
    monkeypatch.setattr(
        grounding.BASE,
        "_git",
        lambda repo, *args: "0123456789abcdef0123456789abcdef01234567",
    )

    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "session-outside",
        "cwd": str(outside),
    }
    result = grounding._session_ground(
        payload,
        "session-outside",
        "codex",
        session_source="startup",
    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "DISH PRE-ROLE SESSION BOOTSTRAP" in context
    marker = json.loads(
        grounding.BASE.marker_path("session-outside").read_text(encoding="utf-8")
    )
    assert marker["status"] == "pre-role"
    assert marker["resolved_role"] is None
    assert marker["workspace"] == str(candidate)
    assert marker["session_grounding"]["repository_head"] == (
        "0123456789abcdef0123456789abcdef01234567"
    )
    assert not grounding.BASE.boundary_path("session-outside").exists()
    assert seen[0] == str(candidate)
    assert str(outside) not in seen
