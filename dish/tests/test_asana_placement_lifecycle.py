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
from tests.support.planning import PLANNING, TASK
from tests.support.placement import (
    StatefulAsanaTransport,
    _release,

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
