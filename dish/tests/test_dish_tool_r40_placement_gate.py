from __future__ import annotations

import copy
from pathlib import Path

import asana
import pytest

from dish_tool.backend import AsanaBackend, close_asana_sdk_client
from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import initialize_database
from dish_tool.models import ResolvedRelease
from test_dish_tool_step6_prepare import PLANNING, TASK


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
            "completed": False, "modified_at": "now", "projects": [{"gid": task["project"]}],
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


@pytest.fixture
def sdk_backend():
    config = asana.Configuration()
    config.return_page_iterator = False
    client = asana.ApiClient(config)
    transport = StatefulAsanaTransport()
    client.call_api = transport.call_api
    backend = AsanaBackend(api_client=client)
    try:
        yield backend, transport
    finally:
        close_asana_sdk_client(client)


def test_real_sdk_full_placement_lifecycle(tmp_path, sdk_backend):
    backend, transport = sdk_backend
    honest = tmp_path / "honest"; honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text("# Verification protocol\n")
    app = DishApplication(initialize_database(tmp_path / "dish.db"), backend, release_loader=lambda role=None: _release(honest, role))

    created = app.execute("create", agent="gpt", title="Bare")
    assert created["ok"]
    task_gid = created["task_gid"]
    assert transport.tasks[task_gid]["section"] == "rq"

    planning = app.execute("start", agent="gpt", task_gid=task_gid, kind="planning", run_id="planning-run")
    planning_file = tmp_path / "planning.txt"
    planning_file.write_text(PLANNING.replace("Sichuan — 12345", "Planned — 333"))
    planned = app.execute("prepare", model="gpt-5.6-sol", agent="gpt", submission_id=planning["submission_id"], file_path=str(planning_file))
    assert planned["ok"], planned
    assert transport.tasks[task_gid]["section"] == "rq"

    research = app.execute("start", agent="gpt", task_gid=task_gid, kind="initial", run_id="research-run")
    candidate = tmp_path / "candidate.txt"
    candidate.write_text(TASK.replace("Sichuan — 12345", "Planned — 333"))
    prepared = app.execute("prepare", model="gpt-5.6-sol", agent="gpt", submission_id=research["submission_id"], file_path=str(candidate))
    assert prepared["ok"] and transport.tasks[task_gid]["section"] == "vq"

    review = app.execute("start", agent="codex", task_gid=task_gid, kind="verification", run_id="verify-run", independence_attestation="independent")
    assert review["ok"]
    review_inspect = app.execute("inspect", agent="codex", submission_id=research["submission_id"])
    assert review_inspect["ok"]
    approved = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=research["submission_id"], correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True,
        provenance_complete=True, run_id="verify-run",
        independence_attestation="independent",
    )
    assert approved["ok"]
    inspected = app.execute("inspect", agent="gpt", submission_id=research["submission_id"])
    assert "submit" in inspected["allowed_actions"], inspected
    submitted = app.execute("submit", submission_id=research["submission_id"])
    assert submitted["ok"], submitted
    assert transport.tasks[task_gid]["section"] == "333"

    placement_calls = [c for c in transport.calls if c[0] == "/sections/{section_gid}/addTask"]
    assert [c[2]["section_gid"] for c in placement_calls] == ["rq", "vq", "333"]
    assert all(c[3] == {"data": {"task": task_gid}} for c in placement_calls)
