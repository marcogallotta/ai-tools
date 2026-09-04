from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .common import fail


V2_LIFECYCLE_SECTIONS = frozenset(
    {
        "Needs Processing",
        "Needs Research",
        "Needs Agentic Review",
        "Needs Human Review",
        "Waiting on Dependency",
        "Ready",
        "Under Development",
        "Needs Post-Merge Rollout",
        "Done",
    }
)

# Executable projection of the repository-owned registry in
# dish/docs/agents/asana-v2-project-mode.md.  Every consumer uses this mapping;
# tests keep the maintained contract and executable projection byte-for-byte aligned.
REGISTERED_V2_PROJECTS: Mapping[str, str] = {
    "1217419962189616": "Dish — Development Workflow",
    "1217404747383060": "Dish — PostgreSQL / Dark Launch",
    "1217382473444945": "Dish — Coordinator",
    "1217381674871544": "Dish — Workflow",
}


@dataclass(frozen=True)
class RegisteredV2Project:
    gid: str
    base_name: str
    live_name: str


def classify_registered_v2_project(
    project: Mapping[str, Any], section_names: set[str]
) -> RegisteredV2Project:
    gid = str(project.get("gid") or "")
    base_name = REGISTERED_V2_PROJECTS.get(gid)
    if base_name is None:
        fail("MUTATION_TASK_AUTHORITY_INVALID", f"owning project {gid or 'unknown'!r} is not registered for V2")

    live_name = str(project.get("name") or "")
    if live_name == base_name:
        fail("MUTATION_TASK_MODE_LEGACY", f"registered project {gid} is in legacy mode, not V2")
    if live_name.startswith(f"{base_name} v") and live_name != f"{base_name} v2":
        fail("MUTATION_TASK_MODE_UNSUPPORTED", f"registered project {gid} has unsupported mode {live_name!r}")
    if live_name != f"{base_name} v2":
        fail("MUTATION_TASK_AUTHORITY_INVALID", f"registered project {gid} identity is unknown or contradictory: {live_name!r}")

    missing = sorted(V2_LIFECYCLE_SECTIONS - section_names)
    if missing:
        fail(
            "MUTATION_TASK_MODE_CONTRADICTORY",
            f"registered project {gid} is missing required V2 sections: {', '.join(missing)}",
        )
    return RegisteredV2Project(gid=gid, base_name=base_name, live_name=live_name)
