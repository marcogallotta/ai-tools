from __future__ import annotations
import uuid
import pytest
from dish_tool.admin import DishAdminApplication
from dish_tool.admin_cli import build_parser
from tests.support.service_scenarios import RUN_ID, post as _post, running as _running
from tests.support.thread_teardown import join_thread, stop_server
from tests.support.submission import _signed

from tests.support.admin_argument_validation import (
    _parse_generated_human_action,
)


def test_human_renderer_surfaces_recovery_actions_from_errors():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": False,
        "command": "authorize-governed-change",
        "code": "VALIDATION_FAILED",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": True,
        "allowed_actions": [],
        "data": {"message": "authorization required"},
        "errors": [
            {
                "rule": "governed_change_unauthorized",
                "human_action": {
                    "kind": "authorize-governed-change",
                    "summary": "Authorize the exact Exemptions change.",
                    "effect": "Create one authorization without editing the task.",
                    "shell_command": "dish-admin authorize-governed-change operation-1 --field Exemptions",
                },
            }
        ],
    }
    rendered = render_admin_result(result, profile="prod")
    assert "Could not authorize-governed-change" in rendered
    assert "Authorize the exact Exemptions change." in rendered
    assert "dish-admin authorize-governed-change operation-1" in rendered
    assert '"errors"' not in rendered

def test_human_renderer_explains_authorization_success_without_claiming_a_write():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "authorize-governed-change",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": False,
        "allowed_actions": [],
        "data": {"field": "Exemptions"},
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Authorization recorded" in rendered
    assert "task itself was not changed" in rendered
    assert "retry the same exact candidate" in rendered

def test_human_renderer_shows_governed_change_details_before_command():
    from dish_tool.admin_human import render_admin_result
    from dish_tool.human_actions import governed_change_action

    spec = governed_change_action(
        operation_id="operation-1",
        field="Exemptions",
        before="None",
        after="[nutrition-kcal] controlled tasting",
    )
    result = {
        "ok": False,
        "command": "reject",
        "code": "VALIDATION_FAILED",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": True,
        "allowed_actions": [],
        "data": {"message": "authorization required"},
        "errors": [{"rule": "governed_change_unauthorized", **spec.payload()}],
    }

    rendered = render_admin_result(result, profile="prod")
    assert rendered.index("Change this task's Exemptions") < rendered.index(
        "Run: dish-admin authorize-governed-change"
    )
    assert "Scope: this task, this operation" in rendered
    assert "does not edit the task or approve Verification" in rendered

def test_human_renderer_summarizes_global_attention_items():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "attention",
        "code": "OK",
        "state": "ok",
        "retryable": False,
        "allowed_actions": [],
        "data": {
            "checked_count": 3,
            "attention_count": 1,
            "healthy_count": 2,
            "category_counts": {
                "safe_cleanup": 0,
                "multi_step_safe": 1,
                "needs_marco": 0,
                "unsafe": 0,
            },
            "attention_items": [
                {
                    "category": "multi_step_safe",
                    "task_title": "Laap gai",
                    "operation_id": "operation-1",
                    "problem": "A dead verifier attempt must be abandoned.",
                    "human_actions": [
                        {
                            "summary": "Abandon the dead verifier attempt.",
                            "shell_command": "dish-admin abandon-operation operation-1 --lease-id lease-1",
                        }
                    ],
                }
            ],
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Dish attention" in rendered
    assert "Workflow records checked: 3" in rendered
    assert "[SAFE MULTI-STEP] Laap gai" in rendered
    assert "dish-admin abandon-operation operation-1" in rendered
