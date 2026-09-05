from __future__ import annotations

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from agent_worktree_lib.asana_v2 import (
    REGISTERED_V2_PROJECTS,
    V2_LIFECYCLE_SECTIONS,
    classify_registered_v2_project,
)
from agent_worktree_lib.common import AgentWorktreeError


def test_all_registered_v2_projects_ignore_extra_sections() -> None:
    sections = set(V2_LIFECYCLE_SECTIONS) | {"Untitled section", "Backlog", "anything else"}

    for gid, base_name in REGISTERED_V2_PROJECTS.items():
        result = classify_registered_v2_project(
            {"gid": gid, "name": f"{base_name} v2"}, sections
        )

        assert result.gid == gid
        assert result.live_name == f"{base_name} v2"


def test_registered_v2_admission_rejects_missing_required_sections() -> None:
    gid = "1217419962189616"
    base_name = REGISTERED_V2_PROJECTS[gid]
    sections = set(V2_LIFECYCLE_SECTIONS) - {"Done"}

    with pytest.raises(AgentWorktreeError) as exc_info:
        classify_registered_v2_project(
            {"gid": gid, "name": f"{base_name} v2"}, sections
        )

    assert exc_info.value.code == "MUTATION_TASK_MODE_CONTRADICTORY"
    assert "Done" in exc_info.value.message
