#!/usr/bin/env python3
# PreToolUse guard: deny every edit-tool write into Claude's auto-memory
# (~/.claude/projects/<project>/memory/...). Ported from switchstand's
# scripts/codex-hook [memory-write] check.
import json
import os
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Write"}


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _memory_target(path: str, memory_root: str) -> bool:
    if not _inside(path, memory_root):
        return False
    parts = Path(os.path.relpath(path, memory_root)).parts
    return len(parts) >= 3 and parts[1] == "memory"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in EDIT_TOOLS:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    try:
        # Check both the literal and the resolved path so a symlinked memory
        # dir pointing elsewhere is still caught.
        literal = os.path.abspath(os.path.join(cwd, os.path.expanduser(raw)))
        target = os.path.realpath(literal)
        home_root = os.path.abspath(os.path.expanduser("~/.claude/projects"))
        memory_root = os.path.realpath(home_root)
        blocked = _memory_target(literal, home_root) or _memory_target(target, memory_root)
    except ValueError:
        blocked = True
    if blocked:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason":
                f"[memory-write] blocked: auto-memory is not allowed ({raw}). "
                "Do not work around this: no Bash, scripts, other tools, or other "
                "paths to write memory or MEMORY.md. If something is worth "
                "remembering, tell Marco in chat and let him decide.",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
