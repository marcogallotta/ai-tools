from __future__ import annotations

import ast
from pathlib import Path


def test_adopt_uses_only_expected_local_git_commands() -> None:
    source = (Path(__file__).resolve().parents[1] / "agent_worktree_lib" / "start_resume.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    commands: list[list[str]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in {"_rollback_local_adoption", "_validate_adoption_remote", "_adopt_remote_branch_locked", "command_adopt"}:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr == "run" and isinstance(func.value, ast.Name) and func.value.id == "runner":
                commands.append([arg.value for arg in call.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)])
    assert any("worktree" in command and "add" in command for command in commands)
    assert {command[0] for command in commands if command} <= {"worktree", "update-ref", "merge-base"}
    assert all("--force" not in command and "-f" not in command for command in commands)
