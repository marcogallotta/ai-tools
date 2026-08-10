from __future__ import annotations
import uuid
import pytest
from dish_tool.admin import DishAdminApplication
from dish_service.admin_cli import build_parser
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
        "Template: dish-admin authorize-governed-change"
    )
    assert "Scope: this task, this operation" in rendered
    assert "does not edit the task or approve Verification" in rendered

def test_human_renderer_labels_input_commands_as_templates_but_exact_recovery_as_runnable():
    from dish_tool.admin_human import render_admin_result

    template_result = {
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "submission_id": "operation-1",
        "allowed_actions": [],
        "data": {
            "problem": "Needs one fact.",
            "human_actions": [{
                "kind": "supply-evidence",
                "summary": "Record the answer.",
                "requires_input": [{"name": "detail"}],
                "shell_command": "dish-admin supply-evidence operation-1 --detail '<answer>'",
            }],
        },
        "errors": [],
    }
    rendered_template = render_admin_result(template_result, profile="prod")
    assert "Template: dish-admin supply-evidence" in rendered_template
    assert "Run: dish-admin supply-evidence" not in rendered_template

    exact_result = {
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "submission_id": "operation-1",
        "allowed_actions": [],
        "data": {
            "problem": "Interrupted execution.",
            "human_actions": [{
                "kind": "reconcile-uncertain-effect",
                "summary": "Recover automatically.",
                "requires_input": [],
                "shell_command": "dish-admin recover operation-1",
            }],
        },
        "errors": [],
    }
    rendered_exact = render_admin_result(exact_result, profile="prod")
    assert "Run: dish-admin recover operation-1" in rendered_exact


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
            "needs_you_count": 1,
            "system_count": 0,
            "healthy_count": 2,
            "category_counts": {"system": 0, "needs_marco": 1, "unsafe": 0},
            "attention_items": [
                {
                    "category": "needs_marco",
                    "needs_you": True,
                    "task_title": "Laap gai",
                    "dish_id": "11111111-1111-4111-8111-111111111111",
                    "signals": [
                        {
                            "summary": "A prior run needs operator replacement.",
                            "shell_command": "dish-admin inspect 11111111-1111-4111-8111-111111111111",
                        }
                    ],
                }
            ],
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Dish attention" in rendered
    assert "Needs you: 1 dish" in rendered
    assert "[NEEDS YOU] Laap gai" in rendered
    assert "dish-admin inspect 11111111-1111-4111-8111-111111111111" in rendered
    assert "Workflow records checked" not in rendered


def test_human_renderer_asks_dead_verifier_decision_before_abandonment_template():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "allowed_actions": [],
        "data": {
            "problem": "The open Verification cycle belongs to a prior run with no active lease.",
            "human_actions": [{
                "kind": "abandon-dead-verifier",
                "summary": "Abandon the dead verifier attempt.",
                "effect": "Preserve the candidate and prepare fresh Verification.",
                "requires_input": [{"name": "reason"}],
                "shell_command": (
                    "dish-admin abandon-operation operation-1 --lease-id lease-1 "
                    "--reason '<why the verifier run is permanently unavailable>'"
                ),
            }],
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Is the previous verifier conversation permanently unavailable?" in rendered
    assert "If yes, Template: dish-admin abandon-operation operation-1" in rendered
    assert "What you can do" not in rendered
    assert "This will:" not in rendered


def test_human_renderer_collapses_normal_recovery_inspection_to_one_command():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "allowed_actions": [],
        "data": {
            "problem": "The prior verifier is inactive, but its interrupted execution must be reconciled.",
            "human_actions": [{
                "kind": "reconcile-before-ownership-transfer",
                "summary": "Automatically reconcile the interrupted execution before ownership moves.",
                "effect": "Settle only proven recovery evidence.",
                "details": [
                    "Automatic inspection is the normal recovery path.",
                    "Manual outcomes are advanced assertions only.",
                ],
                "requires_input": [],
                "shell_command": "dish-admin recover operation-1",
            }],
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Run: dish-admin recover operation-1" in rendered
    assert "What you can do" not in rendered
    assert "Automatic inspection is the normal recovery path" not in rendered
    assert "advanced assertions" not in rendered
    assert "This will:" not in rendered


def test_human_renderer_does_not_turn_plain_recover_legal_actions_into_handoff():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "recover",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "allowed_actions": ["approve", "reject"],
        "data": {},
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "The recovery step completed against the live task." in rendered
    assert "Agent can now" not in rendered
    assert "Tell an agent" not in rendered
    assert "Recovered" not in rendered


@pytest.mark.parametrize("command", ["recover", "recover-lease"])
def test_human_renderer_uses_verified_post_recovery_continuation_for_agent_handoff(command):
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": command,
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "allowed_actions": ["approve", "reject"],
        "data": {
            "post_recovery": {
                "task_title": "Duójiao steamed fish head",
                "phase": "await_verification",
                "administrative_blocker": False,
                "agent_actions_now": ["safe-reclaim"],
                "human_actions": [],
            }
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert 'Tell an agent: "Resume Verification for Duójiao steamed fish head."' in rendered
    assert "Agent can now" not in rendered


@pytest.mark.parametrize("command", ["recover", "recover-lease"])
def test_human_renderer_post_recovery_human_decision_is_not_rendered_as_recovered(command):
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": command,
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "allowed_actions": ["approve", "reject"],
        "data": {
            "post_recovery": {
                "problem": "The previous verifier is inactive.",
                "administrative_blocker": True,
                "agent_actions_now": [],
                "human_actions": [{
                    "kind": "abandon-dead-verifier",
                    "requires_input": [{"name": "reason"}],
                    "shell_command": (
                        "dish-admin abandon-operation operation-1 --lease-id lease-1 "
                        "--reason '<why the verifier run is permanently unavailable>'"
                    ),
                }],
            }
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Recovery step completed" in rendered
    assert "Is the previous verifier conversation permanently unavailable?" in rendered
    assert "If yes, Template: dish-admin abandon-operation operation-1" in rendered
    assert "Recovered" not in rendered
    assert "Tell an agent" not in rendered


def test_human_renderer_connected_agent_continuation_is_a_concrete_handoff():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "abandon-operation",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "allowed_actions": ["start"],
        "data": {
            "required_action": {
                "surface": "connected-agent",
                "command": "start",
                "arguments": {"kind": "verification"},
            }
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert 'Tell an agent: "Resume Verification for task 121."' in rendered
    assert "Agent can now" not in rendered


def test_agent_handoff_uses_canonical_dish_uuid_not_title():
    from dish_tool.admin_human import render_admin_result

    dish_id = "11111111-1111-4111-8111-111111111111"
    result = {
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "task_gid": "1217304073198491",
        "allowed_actions": [],
        "data": {
            "dish_id": dish_id,
            "task_title": "Ambiguous title",
            "phase": "await_verification",
            "problem": "Ready for a fresh verifier.",
            "human_actions": [],
            "agent_actions_now": [{"command": "safe-reclaim", "arguments": {}}],
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert f'Tell an agent: "Resume Verification for dish {dish_id}."' in rendered
    assert 'Resume Verification for Ambiguous title' not in rendered


def test_active_leases_renderer_keeps_raw_ids_in_verbose_output():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True, "command": "active-leases", "code": "OK", "allowed_actions": [],
        "data": {
            "state_counts": {"active": 1, "expired": 0, "revoked": 0},
            "leases": [{
                "task_title": "Mapo tofu", "dish_id": "11111111-1111-4111-8111-111111111111",
                "stage": "await_verification", "authority_state": "active",
                "operation_id": "operation-1", "owner_id": "owner-1", "run_id": "run-1",
                "lease_id": "lease-1", "acquired_at": "a", "renewed_at": "b", "expires_at": "c",
            }],
        },
        "errors": [],
    }
    normal = render_admin_result(result, profile="prod")
    verbose = render_admin_result(result, profile="prod", verbose=True)
    assert "[ACTIVE] Mapo tofu" in normal
    assert "Run: run-1" not in normal
    assert "Lease: lease-1" not in normal
    assert "Run: run-1" in verbose
    assert "Lease: lease-1" in verbose


def test_attention_renderer_hides_system_items_until_verbose():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True, "command": "attention", "code": "OK", "allowed_actions": [],
        "data": {
            "checked_count": 2, "live_inspection_count": 0, "needs_you_count": 0,
            "system_count": 1, "category_counts": {"system": 1, "needs_marco": 0, "unsafe": 0},
            "attention_items": [{
                "category": "system", "needs_you": False, "task_title": "Quiet dish",
                "signals": [{"summary": "Inactive run can be resumed by an agent."}],
            }],
        }, "errors": [],
    }
    normal = render_admin_result(result, profile="prod")
    verbose = render_admin_result(result, profile="prod", verbose=True)
    assert "Quiet dish" not in normal
    assert "System/recoverable items are hidden" in normal
    assert "[SYSTEM] Quiet dish" in verbose
    assert "live task inspections: 0" in verbose
