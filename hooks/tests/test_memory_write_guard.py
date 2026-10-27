from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


HOOK = Path(__file__).resolve().parents[1] / "memory-write-guard.sh"


def _run(home: Path, tool: str, field: str, path: str) -> dict:
    proc = subprocess.run(
        [str(HOOK)],
        input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                          "tool_input": {field: path}, "cwd": str(home)}),
        env=os.environ | {"HOME": str(home)},
        text=True, capture_output=True, check=True,
    )
    return json.loads(proc.stdout) if proc.stdout else {}


@pytest.mark.parametrize(("tool", "field", "path"), [
    ("Write", "file_path", ".claude/projects/p/memory/MEMORY.md"),
    ("Edit", "file_path", ".claude/projects/p/memory/fact.md"),
    ("MultiEdit", "file_path", ".claude/projects/p/memory/fact.md"),
    ("NotebookEdit", "notebook_path", ".claude/projects/p/memory/notes.ipynb"),
])
def test_denies_edit_tools_writing_auto_memory(tmp_path, tool, field, path):
    output = _run(tmp_path, tool, field, str(tmp_path / path))["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "[memory-write]" in output["permissionDecisionReason"]
    assert "Do not work around this" in output["permissionDecisionReason"]


def test_denies_relative_memory_path(tmp_path):
    output = _run(tmp_path, "Write", "file_path", ".claude/projects/p/memory/x.md")
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("path", [
    ".claude/projects/p/settings.json",
    "repo/memory/notes.md",
])
def test_allows_non_memory_paths(tmp_path, path):
    assert _run(tmp_path, "Write", "file_path", str(tmp_path / path)) == {}


def test_denies_write_through_symlinked_memory_dir(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    project = tmp_path / ".claude/projects/p"
    project.mkdir(parents=True)
    (project / "memory").symlink_to(outside)
    output = _run(tmp_path, "Write", "file_path", str(project / "memory/x.md"))
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("payload", ["[]", '{"tool_name": "Write", "tool_input": "x"}'])
def test_malformed_payload_does_not_crash(tmp_path, payload):
    proc = subprocess.run([str(HOOK)], input=payload, env=os.environ | {"HOME": str(tmp_path)},
                          text=True, capture_output=True)
    assert proc.returncode == 0 and proc.stdout == ""


def test_ignores_non_edit_tools_and_adjacent_names(tmp_path):
    assert _run(tmp_path, "Read", "file_path",
                str(tmp_path / ".claude/projects/p/memory/x.md")) == {}
    assert _run(tmp_path, "Write", "file_path",
                str(tmp_path / ".claude/projects/p/memory-notes.md")) == {}
