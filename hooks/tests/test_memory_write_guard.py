"""Exercise the executable Claude hook with isolated home directories."""
import json
import os
from pathlib import Path
import subprocess

import pytest

HOOK = Path(__file__).resolve().parents[1] / "memory-write-guard.py"


def run_hook(tmp_path, payload):
    result = subprocess.run(
        [str(HOOK)], input=json.dumps(payload), text=True, capture_output=True,
        env={**os.environ, "HOME": str(tmp_path)}, check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "NotebookEdit", "Write"])
def test_denies_memory_writes(tmp_path, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    result = run_hook(tmp_path, {
        "tool_name": tool,
        "tool_input": {key: "~/.claude/projects/example/memory/MEMORY.md"},
    })
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_relative_target(tmp_path):
    result = run_hook(tmp_path, {
        "tool_name": "Write", "cwd": str(tmp_path),
        "tool_input": {"file_path": ".claude/projects/example/memory/nested/note.md"},
    })
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("outward", [False, True])
def test_symlinks(tmp_path, outward):
    memory = tmp_path / ".claude/projects/example/memory"
    memory.parent.mkdir(parents=True)
    alias = tmp_path / "alias"
    if outward:
        alias.mkdir()
        memory.symlink_to(alias, target_is_directory=True)
        target = memory / "note.md"
    else:
        memory.mkdir()
        alias.symlink_to(memory, target_is_directory=True)
        target = alias / "note.md"
    result = run_hook(tmp_path, {
        "tool_name": "Write", "tool_input": {"file_path": str(target)},
    })
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("path", [
    "project/MEMORY.md", ".claude/projects/example/memory-other/note.md",
    ".claude/projects/example/notes/memory/note.md",
])
def test_allows_non_memory_files(tmp_path, path):
    assert run_hook(tmp_path, {
        "tool_name": "Write", "cwd": str(tmp_path), "tool_input": {"file_path": path},
    }) is None


@pytest.mark.parametrize("payload", [None, {}, {"tool_name": "Read"},
    {"tool_name": "Write", "tool_input": {}},
])
def test_ignores_unrelated_or_missing_input(tmp_path, payload):
    assert run_hook(tmp_path, payload) is None
