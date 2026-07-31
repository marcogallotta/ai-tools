import inspect
import dataclasses
import pytest
import asana

from dish_tool.backend import AsanaBackend
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.errors import DishRuleError
from dish_tool.governed_diff import explicit_material_reasons, require_small_scope
from dish_tool.task_document import parse_task_document
from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions
from tests.support.planning import TASK


def _doc(text=TASK):
    return parse_task_document(text)


def test_quantity_change_is_always_material_and_not_small():
    before = _doc()
    after = _doc(TASK.replace("Portions: one sitting\n100 g test ingredient", "Portions: four sittings\n500 g test ingredient"))
    assert "quantities" in explicit_material_reasons(before, after)
    with pytest.raises(DishRuleError) as exc:
        require_small_scope(before, after)
    assert exc.value.rule == "large_correction_required"


def test_pending_steps_suppress_ordinary_actions():
    snapshot = WorkflowSnapshot(
        operation_status="open", operation_phase="await_verification",
        persisted_actions=("verify",), live_status="pending-verification",
        live_section_gid="vq", verification_queue_gid="vq", cycle_reviewed=False,
        latest_cycle_outcome=None, latest_cycle_route=None, validation_rules=(),
        pending_steps=("route_new_cycle",),
    )
    assert legal_actions(snapshot) == []


def test_installed_asana_section_signature_matches_contract():
    assert tuple(inspect.signature(asana.SectionsApi.add_task_for_section).parameters)[:3] == (
        "self", "section_gid", "opts"
    )


def test_backend_move_uses_real_sdk_shape_and_rereads(monkeypatch):
    backend = AsanaBackend(api_client=object())
    reads = iter([
        {"gid":"t", "name":"Dish", "notes":"N", "memberships":[{"project":{"gid":COOKING_PROJECT_GID},"section":{"gid":"old"}}]},
        {"gid":"t", "name":"Dish", "notes":"N", "memberships":[{"project":{"gid":COOKING_PROJECT_GID},"section":{"gid":"new"}}]},
    ])
    monkeypatch.setattr(backend, "read_task", lambda gid: next(reads))
    seen = {}
    def fake_call(fn, *args, **kwargs):
        seen["name"] = fn.__name__; seen["args"] = args
        return {}
    monkeypatch.setattr(backend, "call", fake_call)
    backend.move_task_to_section(task_gid="t", section_gid="new")
    assert seen == {"name":"add_task_for_section", "args":("new", {"body":{"data":{"task":"t"}}})}
