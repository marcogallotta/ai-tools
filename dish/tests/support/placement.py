from __future__ import annotations


"""Shared helpers extracted from test_asana_placement_lifecycle.py."""


import copy

from pathlib import Path

import asana

import pytest

from dish_tool.backend import AsanaBackend, close_asana_sdk_client

from dish_tool.commands import DishApplication

from dish_tool.constants import COOKING_PROJECT_GID

from dish_tool.database_initialization import initialize_database

from dish_tool.models import ResolvedRelease
from tests.support.planning import PLANNING, TASK


class StatefulAsanaTransport:
    """Stateful low-level transport used by the real generated SDK methods."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.next_id = 1
        self.calls: list[tuple[str, str, dict, object]] = []
        self.sections = [
            {"gid": "rq", "name": "Research Queue"},
            {"gid": "vq", "name": "Verification Queue"},
            {"gid": "333", "name": "Planned"},
            {"gid": "ref", "name": "Reference"},
            {"gid": "src", "name": "Sourcing"},
        ]

    def _task_response(self, task: dict) -> dict:
        section = task.get("section")
        memberships = []
        if task.get("project"):
            memberships.append({
                "project": {"gid": task["project"], "name": "Cooking"},
                "section": None if section is None else {"gid": section, "name": next(s["name"] for s in self.sections if s["gid"] == section)},
            })
        return {
            "gid": task["gid"], "name": task["name"], "notes": task.get("notes", ""),
            "completed": bool(task.get("completed", False)), "modified_at": "now", "projects": [{"gid": task["project"]}],
            "memberships": memberships,
        }

    def call_api(self, resource_path, http_method, path_params, query_params, header_params, *, body=None, **kwargs):
        self.calls.append((resource_path, http_method, dict(path_params), copy.deepcopy(body)))
        if resource_path == "/projects/{project_gid}/sections" and http_method == "GET":
            return {"data": copy.deepcopy(self.sections)}
        if resource_path == "/tasks" and http_method == "POST":
            gid = str(9000000000000000 + self.next_id); self.next_id += 1
            data = body["data"]
            task = {"gid": gid, "name": data["name"], "notes": data.get("notes", ""), "project": data["projects"][0], "section": None}
            self.tasks[gid] = task
            return {"data": self._task_response(task)}
        if resource_path == "/sections/{section_gid}/addTask" and http_method == "POST":
            task_gid = body["data"]["task"]
            self.tasks[task_gid]["section"] = path_params["section_gid"]
            return {"data": {}}
        if resource_path == "/tasks/{task_gid}" and http_method == "GET":
            return {"data": self._task_response(self.tasks[path_params["task_gid"]])}
        if resource_path == "/tasks/{task_gid}" and http_method == "PUT":
            task = self.tasks[path_params["task_gid"]]
            task.update(body["data"])
            return {"data": {"gid": task["gid"]}}
        raise AssertionError(f"unexpected SDK request: {http_method} {resource_path} {path_params} {body}")

def _release(root: Path, role: str | None = None) -> ResolvedRelease:
    protocol = (root / "dish-verification-protocol.md").read_text()
    return ResolvedRelease(
        version="1.0.10", commit="", root=root,
        protocols={} if role is None else {role: protocol if role == "verification" else f"{role} protocol"},
        manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}",
        migration_metadata={}, requested_protocol_role=role,
    )
