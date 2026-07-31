from dish_tool.constants import COOKING_PROJECT_GID
from tests.support.asana_backend import StatefulAsanaBackend


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
