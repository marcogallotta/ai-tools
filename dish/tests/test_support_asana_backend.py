import asana
import pytest

from dish_tool.backend import AsanaBackend, close_asana_sdk_client
from dish_tool.constants import COOKING_PROJECT_GID
from tests.support.asana_backend import DEFAULT_SECTIONS, StatefulAsanaBackend
from tests.support.placement import StatefulAsanaTransport


def test_stateful_asana_backend_preserves_adapter_task_shape_and_mutations():
    backend = StatefulAsanaBackend(title="Dish", notes="old", section="rq")

    backend.update_task_content(task_gid="task", title="Changed", notes="new")
    backend.update_task_completed(task_gid="task", completed=True)
    backend.move_task_to_section(task_gid="task", section_gid="vq")

    assert backend.read_task("task") == {
        "gid": "task",
        "name": "Changed",
        "notes": "new",
        "completed": True,
        "modified_at": "now",
        "projects": [{"gid": COOKING_PROJECT_GID}],
        "memberships": [
            {
                "project": {"gid": COOKING_PROJECT_GID},
                "section": {"gid": "vq"},
            }
        ],
    }
    assert backend.writes == 2
    assert backend.moves == 1


def test_stateful_asana_backend_hooks_and_forbidden_operations_are_deterministic():
    backend = StatefulAsanaBackend()
    observed = []
    backend.before("update_task_content", lambda **arguments: observed.append(arguments["title"]))
    backend.update_task_content(task_gid="task", title="Changed", notes="new")
    backend.forbid("move_task_to_section", "movement is outside this scenario")

    assert observed == ["Changed"]
    assert backend.calls("update_task_content")[0].arguments["notes"] == "new"

    try:
        backend.move_task_to_section(task_gid="task", section_gid="vq")
    except AssertionError as exc:
        assert str(exc) == "movement is outside this scenario"
    else:
        raise AssertionError("forbidden operation did not fail")


def test_stateful_asana_backend_supports_multiple_fixture_tasks():
    backend = StatefulAsanaBackend(
        tasks=[
            {"task_gid": "one", "title": "One", "notes": "a", "section_gid": "rq"},
            {"task_gid": "two", "title": "Two", "notes": "b", "section_gid": "vq"},
        ]
    )

    backend.move_task_to_section(task_gid="one", section_gid="12345")

    assert backend.read_task("one")["memberships"][0]["section"]["gid"] == "12345"
    assert backend.read_task("two")["memberships"][0]["section"]["gid"] == "vq"


def test_stateful_asana_backend_rejects_unknown_task_identity():
    backend = StatefulAsanaBackend(task_gid="known")

    with pytest.raises(AssertionError, match="unexpected task gid: missing"):
        backend.read_task("missing")
    with pytest.raises(AssertionError, match="unexpected task gid: missing"):
        backend.update_task_content(task_gid="missing", title="x", notes="y")

    assert set(backend.tasks) == {"known"}


def _contract_snapshot(backend):
    sections = backend.list_sections(COOKING_PROJECT_GID)
    before = backend.read_task("t")
    backend.update_task_content(task_gid="t", title="Changed", notes="new")
    backend.update_task_completed(task_gid="t", completed=True)
    backend.move_task_to_section(task_gid="t", section_gid="vq")
    after = backend.read_task("t")
    created = backend.create_bare_task(
        title="Created", project_gid=COOKING_PROJECT_GID, section_gid="rq"
    )
    return {
        "sections": [(item["gid"], item["name"]) for item in sections],
        "before": {
            "gid": before["gid"],
            "name": before["name"],
            "notes": before["notes"],
            "completed": before["completed"],
            "section": before["memberships"][0]["section"]["gid"],
        },
        "after": {
            "gid": after["gid"],
            "name": after["name"],
            "notes": after["notes"],
            "completed": after["completed"],
            "section": after["memberships"][0]["section"]["gid"],
        },
        "created": {
            "gid": created["gid"],
            "name": created["name"],
            "notes": created["notes"],
            "completed": created["completed"],
            "section": created["memberships"][0]["section"]["gid"],
        },
    }


def test_stateful_fake_matches_real_sdk_adapter_contract():
    fake = StatefulAsanaBackend(
        title="Dish",
        notes="old",
        section="rq",
        task_gid="t",
        created_task_gid="9000000000000001",
    )

    config = asana.Configuration()
    config.return_page_iterator = False
    client = asana.ApiClient(config)
    transport = StatefulAsanaTransport()
    transport.sections = [dict(item) for item in DEFAULT_SECTIONS]
    transport.tasks["t"] = {
        "gid": "t",
        "name": "Dish",
        "notes": "old",
        "completed": False,
        "project": COOKING_PROJECT_GID,
        "section": "rq",
    }
    client.call_api = transport.call_api
    sdk = AsanaBackend(api_client=client)
    try:
        assert _contract_snapshot(fake) == _contract_snapshot(sdk)
    finally:
        close_asana_sdk_client(client)
