"""Scope user-level Codex hooks to the primary ai-tools checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any


AI_TOOLS_ROOT = Path.home().resolve() / "ai-tools"


def applies(payload: dict[str, Any]) -> bool:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False
    resolved = Path(cwd).expanduser().resolve()
    return resolved == AI_TOOLS_ROOT or AI_TOOLS_ROOT in resolved.parents
