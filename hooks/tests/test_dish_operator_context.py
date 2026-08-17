from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "dish-operator-context"
STYLE = ROOT / ".claude" / "output-styles" / "dish-operator.md"


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
