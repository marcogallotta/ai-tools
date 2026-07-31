from __future__ import annotations

import copy

import asana
import pytest

from dish_tool.constants import COOKING_PROJECT_GID, REFERENCE_SECTION_GID
from dish_tool.backend import close_asana_sdk_client
from dish_tool.generic_asana_guard import CookingMutationBlocked, CookingMutationGuard


class GuardTransport:
    """Real generated SDK methods over a controlled low-level transport."""

    def __init__(self):
        self.calls = []
        self.tasks = {
            "100": {
                "memberships": [{
                    "project": {"gid": COOKING_PROJECT_GID},
                    "section": {"gid": "700", "name": "Research Queue"},
                }]
            },
            "101": {
                "memberships": [{
                    "project": {"gid": COOKING_PROJECT_GID},
                    "section": {"gid": REFERENCE_SECTION_GID, "name": "Reference"},
                }]
            },
            "200": {
                "memberships": [{
                    "project": {"gid": "999"},
                    "section": {"gid": "800", "name": "Other"},
                }]
            },
        }
        self.sections = {
            "700": {"gid": "700", "project": {"gid": COOKING_PROJECT_GID}},
            REFERENCE_SECTION_GID: {
                "gid": REFERENCE_SECTION_GID,
                "project": {"gid": COOKING_PROJECT_GID},
            },
            "800": {"gid": "800", "project": {"gid": "999"}},
        }

    def call_api(
        self,
        resource_path,
        http_method,
        path_params,
        query_params,
        header_params,
        *,
        body=None,
        **kwargs,
    ):
        self.calls.append((resource_path, http_method, dict(path_params), copy.deepcopy(body)))
        if resource_path == "/tasks/{task_gid}" and http_method == "GET":
            gid = path_params["task_gid"]
            if gid == "500":
                raise RuntimeError("lookup unavailable")
            return {"data": copy.deepcopy(self.tasks[gid])}
        if resource_path == "/sections/{section_gid}" and http_method == "GET":
            gid = path_params["section_gid"]
            if gid == "900":
                raise RuntimeError("lookup unavailable")
            return {"data": copy.deepcopy(self.sections[gid])}
        raise AssertionError((resource_path, http_method, path_params, body, kwargs))


@pytest.fixture
def guard_transport():
    config = asana.Configuration()
    config.return_page_iterator = False
    client = asana.ApiClient(config)
    transport = GuardTransport()
    client.call_api = transport.call_api
    try:
        yield CookingMutationGuard(api_client=client), transport
    finally:
        close_asana_sdk_client(client)


@pytest.mark.smoke
def test_real_sdk_guard_blocks_managed_task_but_allows_excluded_and_outside(guard_transport):
    guard, transport = guard_transport
    with pytest.raises(CookingMutationBlocked, match="managed_section"):
        guard.before_task_mutation("100", command="set-notes")
    guard.before_task_mutation("101", command="set-notes")
    guard.before_task_mutation("200", command="set-notes")
    assert [call[0] for call in transport.calls] == [
        "/tasks/{task_gid}",
        "/tasks/{task_gid}",
        "/tasks/{task_gid}",
    ]


@pytest.mark.smoke
def test_lookup_failure_fails_closed_before_generic_write(guard_transport):
    guard, _transport = guard_transport
    with pytest.raises(CookingMutationBlocked, match="task_lookup_unresolved"):
        guard.before_task_mutation("500", command="rename")


@pytest.mark.smoke
def test_move_blocks_source_or_destination_crossing_governed_boundary(guard_transport):
    guard, _transport = guard_transport
    with pytest.raises(CookingMutationBlocked, match="managed_section"):
        guard.before_move(task_gid="100", section_gid="800", command="move")
    with pytest.raises(CookingMutationBlocked, match="managed_section"):
        guard.before_move(task_gid="200", section_gid="700", command="move")
    guard.before_move(task_gid="200", section_gid="800", command="move")


@pytest.mark.smoke
def test_create_and_subtask_guard_cover_governed_cooking_targets(guard_transport):
    guard, _transport = guard_transport
    with pytest.raises(CookingMutationBlocked):
        guard.before_create_task(
            project_gid=COOKING_PROJECT_GID,
            section_gid="700",
            command="create-task",
        )
    guard.before_create_task(
        project_gid=COOKING_PROJECT_GID,
        section_gid=REFERENCE_SECTION_GID,
        command="create-task",
    )
    with pytest.raises(CookingMutationBlocked):
        guard.before_create_subtask(parent_gid="100", command="create-subtask")


@pytest.mark.smoke
def test_raw_task_and_section_mutations_are_guarded(guard_transport):
    guard, _transport = guard_transport
    with pytest.raises(CookingMutationBlocked):
        guard.before_raw(method="PUT", path="/tasks/100", payload={"completed": True})
    with pytest.raises(CookingMutationBlocked):
        guard.before_raw(
            method="POST",
            path="/sections/700/addTask",
            payload={"task": "200"},
        )
    guard.before_raw(method="PUT", path="/tasks/200", payload={"completed": True})
