from __future__ import annotations

from pathlib import Path


def propagate_takeover_to_wrapped_resume(argv: list[str]) -> list[str]:
    """Carry outer claim takeover intent into its nested agent-worktree resume."""
    normalized = list(argv)
    if len(normalized) < 2 or normalized[1] != "claim" or "--takeover" not in normalized:
        return normalized
    try:
        separator = normalized.index("--")
    except ValueError:
        return normalized
    child = normalized[separator + 1 :]
    for index in range(len(child) - 1):
        if Path(child[index]).name != "agent-worktree" or child[index + 1] != "resume":
            continue
        if "--takeover" not in child[index + 2 :]:
            child.insert(index + 2, "--takeover")
            normalized[separator + 1 :] = child
        break
    return normalized
