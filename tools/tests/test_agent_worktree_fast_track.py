from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_worktree_lib import fast_track, fast_track_guard  # noqa: E402
from agent_worktree_lib.common import AgentWorktreeError  # noqa: E402


BASE = "1" * 40
TASK = "1217454324557309"


def _story(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "task": TASK,
        "mode": "TRIVIAL",
        "branch": "agent/tiny-change",
        "base_ref": "refs/heads/main",
        "base_head": BASE,
        "paths": ["docs/tiny.md"],
        "marco_words": "TRIVIAL this exact change",
        "skip_review": True,
        "validation": "meaningful-readback",
    }
    body.update(overrides)
    import json

    marker = "<!-- dish-fast-track-authorization:v1 " + json.dumps(body, separators=(",", ":"), sort_keys=True) + " -->"
    return {"gid": "story-42", "text": marker}


def _assert_code(exc: pytest.ExceptionInfo[AgentWorktreeError], code: str) -> None:
    assert exc.value.code == code


def test_explicit_authorization_is_exact_and_cli_requires_existing_story():
    auth = fast_track._parse_authorization_story(_story(), TASK)
    assert auth.mode == "TRIVIAL"
    assert auth.branch == "agent/tiny-change"
    assert auth.base_head == BASE
    assert auth.paths == ("docs/tiny.md",)
    assert auth.marco_words == "TRIVIAL this exact change"
    assert auth.skip_review is True
    assert auth.validation == "meaningful-readback"

    parsed = fast_track.build_fast_track_parser().parse_args(
        ["fast-track-commit", "--task", TASK, "--authorization-story", "story-42", "-m", "tiny"]
    )
    assert parsed.authorization_story == "story-42"


def test_no_self_authorization_without_preexisting_story():
    with pytest.raises(AgentWorktreeError) as exc:
        fast_track._live_authorization(TASK, None, {})
    _assert_code(exc, "FAST_TRACK_AUTHORIZATION_REQUIRED")


def test_authorization_rejects_path_traversal():
    with pytest.raises(AgentWorktreeError) as exc:
        fast_track._parse_authorization_story(_story(paths=["../escape"]), TASK)
    _assert_code(exc, "FAST_TRACK_AUTHORIZATION_INVALID")


def test_changed_path_escape_falls_back_to_normal_lifecycle():
    auth = fast_track._parse_authorization_story(_story(), TASK)
    with pytest.raises(AgentWorktreeError) as exc:
        fast_track._require_bounded({"docs/tiny.md", "outside.txt"}, auth, label="worktree change")
    _assert_code(exc, "FAST_TRACK_FALLBACK_REQUIRED")
    assert "normal lifecycle" in exc.value.message


def test_protected_primary_checkout_is_refused():
    primary = Path("/repo")
    repo = SimpleNamespace(primary_top=primary)
    identity = SimpleNamespace(path=primary, git_dir=primary / ".git", common_dir=primary / ".git", branch="main")
    with pytest.raises(AgentWorktreeError) as exc:
        fast_track.assert_fast_track_worktree(identity, repo)
    _assert_code(exc, "PROTECTED_PRIMARY")


def test_stale_authorized_main_falls_back(monkeypatch: pytest.MonkeyPatch):
    auth = fast_track._parse_authorization_story(_story(), TASK)
    monkeypatch.setattr(fast_track_guard, "remote_ref_sha", lambda runner, repo, ref: "2" * 40)
    with pytest.raises(AgentWorktreeError) as exc:
        fast_track._require_fresh_base(SimpleNamespace(), SimpleNamespace(), auth)
    _assert_code(exc, "FAST_TRACK_FALLBACK_REQUIRED")
    assert "stale" in exc.value.message


def test_high_consequence_path_falls_back_instead_of_widening_scope():
    auth = fast_track._parse_authorization_story(_story(paths=["dish/docs/agents/policy.md"]), TASK)
    with pytest.raises(AgentWorktreeError) as exc:
        fast_track._require_bounded({"dish/docs/agents/policy.md"}, auth, label="worktree change")
    _assert_code(exc, "FAST_TRACK_FALLBACK_REQUIRED")

def test_fast_track_product_runtime_can_use_explicit_executable_proof_class():
    auth = fast_track._parse_authorization_story(
        _story(mode="FAST-TRACK", paths=["dish/dish_pg/runtime.py"], validation="executable-proof"), TASK
    )
    fast_track._require_bounded({"dish/dish_pg/runtime.py"}, auth, label="published change")
    assert auth.validation == "executable-proof"


def test_fast_track_docs_can_use_meaningful_readback_without_irrelevant_test_gate():
    auth = fast_track._parse_authorization_story(
        _story(mode="FAST-TRACK", paths=["docs/wording.md"], validation="meaningful-readback"), TASK
    )
    fast_track._require_bounded({"docs/wording.md"}, auth, label="published change")
    assert auth.validation == "meaningful-readback"
