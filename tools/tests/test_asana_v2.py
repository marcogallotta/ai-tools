from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from agent_worktree_lib.asana_v2 import V2_LIFECYCLE_SECTIONS, classify_registered_v2_project


def test_registered_v2_admission_ignores_extra_sections() -> None:
    project = {"gid": "1217381674871544", "name": "Dish — Workflow v2"}
    sections = set(V2_LIFECYCLE_SECTIONS) | {"Untitled section", "Backlog", "anything else"}

    result = classify_registered_v2_project(project, sections)

    assert result.gid == "1217381674871544"
    assert result.live_name == "Dish — Workflow v2"
