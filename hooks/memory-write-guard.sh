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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    event = payload.get("hook_event_name") or "PreToolUse"
    if event not in {"PreToolUse", "PermissionRequest"} or payload.get("tool_name") not in EDIT_TOOLS:
        return 0
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    target = os.path.realpath(os.path.join(cwd, os.path.expanduser(raw)))
    memory_root = os.path.realpath(os.path.expanduser("~/.claude/projects"))
    if _inside(target, memory_root):
        parts = Path(os.path.relpath(target, memory_root)).parts
        if len(parts) >= 3 and parts[1] == "memory":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": event, "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"[memory-write] blocked: auto-memory is not allowed ({target}).",
            }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
