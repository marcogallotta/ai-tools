from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "dish-operator-context"
STYLE = ROOT / ".claude" / "output-styles" / "dish-operator.md"
CODEX_README = ROOT / "codex" / "README.md"
OPERATOR_ADAPTER = "/home/marco/.local/bin/dish-operator-context"


def _style_body() -> str:
    text = STYLE.read_text(encoding="utf-8")
    return text.split("---\n", 2)[2].strip()


def test_codex_session_start_injects_same_operator_policy():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert output["additionalContext"] == "DISH OPERATOR INTERACTION POLICY\n\n" + _style_body()


def test_codex_hooks_load_operator_policy_on_every_session_start():
    settings = json.loads((ROOT / "codex/hooks.json").read_text(encoding="utf-8"))
    session_hooks = settings["hooks"]["SessionStart"]
    operator_entries = [
        entry
        for entry in session_hooks
        if any("dish-operator-context" in hook.get("command", "") for hook in entry.get("hooks", []))
    ]
    assert len(operator_entries) == 1
    assert "matcher" not in operator_entries[0]
    assert operator_entries[0]["hooks"][0]["command"] == f"python3 {OPERATOR_ADAPTER}"


def test_operator_adapter_symlink_resolves_policy_from_candidate(tmp_path):
    adapter = tmp_path / "dish-operator-context"
    adapter.symlink_to(HOOK)
    proc = subprocess.run(
        [sys.executable, str(adapter)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert output == "DISH OPERATOR INTERACTION POLICY\n\n" + _style_body()


def test_exact_head_certification_binds_operator_adapter_to_candidate_worktree():
    readme = CODEX_README.read_text(encoding="utf-8")
    assert (
        'ln -s "$WT/hooks/dish-operator-context" /home/marco/.local/bin/dish-operator-context'
        in readme
    )
    assert (
        'test "$(readlink -f /home/marco/.codex/hooks.json)" = "$WT/codex/hooks.json"'
        in readme
    )
    assert (
        'test "$(readlink -f /home/marco/.local/bin/dish-operator-context)" = '
        '"$WT/hooks/dish-operator-context"'
        in readme
    )


def test_operator_context_hook_ignores_non_session_events():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "PreToolUse"}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
