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


def test_governed_exemptions_action_explains_approval_and_roundtrips_parser():
    from dish_tool.human_actions import governed_change_action, relay_text

    after = (
        "[nutrition-kcal] Scope: this tasting may remain below 700 kcal. | "
        "[nutrition-protein] Scope: this tasting may remain below 35g protein."
    )
    spec = governed_change_action(
        operation_id=str(uuid.uuid4()),
        field="Exemptions",
        before="None",
        after=after,
    )
    action = spec.payload()["human_action"]

    parsed = _parse_generated_human_action(action)
    assert parsed.command == "authorize-governed-change"
    assert parsed.field == "Exemptions"
    assert parsed.before == "None"
    assert parsed.after == after
    assert action["context"]["governed_change"]["added_tokens"] == [
        "nutrition-kcal",
        "nutrition-protein",
    ]
    details = "\n".join(action["details"])
    assert "700–1,000 kcal" in details
    assert "minimum 35 g protein" in details
    assert "this task, this operation" in details
    assert "does not edit the task or approve Verification" in details
    assert "retry the same unchanged candidate" in details

    relay = relay_text(spec, instruction="Wait for Marco, then retry.")
    assert relay.index("Before the command") < relay.index("dish-admin authorize-governed-change")
    assert "[nutrition-kcal]" in relay
    assert "[nutrition-protein]" in relay

def test_semantic_review_queue_commands_are_first_class_admin_commands():
    proposal_id = str(uuid.uuid4())
    assert build_parser().parse_args(["review-queue"]).command == "review-queue"
    assert build_parser().parse_args(["review-inspect", proposal_id]).proposal_id == proposal_id
    approved = build_parser().parse_args(["review-approve", proposal_id])
    assert approved.command == "review-approve"
    # The legacy CLI may still inject the semantic-proposal default reason. The
    # application layer must reject that synthetic text for Human Review items.
    assert approved.reason == "Approved after reviewing the exact linked change bundle."
    rejected = build_parser().parse_args([
        "review-reject", proposal_id, "--reason", "wrong interpretation"
    ])
    assert rejected.reason == "wrong interpretation"

def test_human_renderer_explains_semantic_proposal_before_approval_commands():
    from dish_tool.admin_human import render_admin_result

    proposal_id = str(uuid.uuid4())
    result = {
        "ok": True,
        "command": "review-inspect",
        "code": "OK",
        "state": "pending",
        "retryable": False,
        "allowed_actions": [],
        "data": {
            "proposal": {
                "proposal_id": proposal_id,
                "status": "pending",
                "explanation": {
                    "problem": "The title still requires home-grown scallion greens.",
                    "cause": "A settled Marco decision made shop-bought whole scallion the default.",
                    "why_not_ordinary_correction": "Dish candidate is governed.",
                    "recommended_resolution": "Remove the stale harvest dependency everywhere it appears.",
                    "scope": "This task and exact candidate only.",
                    "after_success": "A fresh agent applies the exact stored candidate.",
                },
                "changes": [
                    {
                        "field": "Dish candidate",
                        "before": "[Scallion greens] Vietnamese scallion egg",
                        "after": "Vietnamese scallion egg",
                    },
                    {
                        "field": "Locks",
                        "before": "Harvest home-grown greens",
                        "after": "Use shop-bought whole scallion",
                    },
                ],
                "linked_changes": [
                    {
                        "path": "title",
                        "before": "[Scallion greens] Vietnamese scallion egg",
                        "after": "Vietnamese scallion egg",
                    },
                    {
                        "path": "planning.Locks",
                        "before": "Harvest home-grown greens",
                        "after": "Use shop-bought whole scallion",
                    },
                ],
            }
        },
        "errors": [],
    }
    rendered = render_admin_result(result, profile="prod")
    assert "Governed changes" in rendered
    assert "Dish candidate" in rendered and "Locks" in rendered
    assert "Complete linked candidate change set" in rendered
    assert '- title: "[Scallion greens] Vietnamese scallion egg" -> "Vietnamese scallion egg"' in rendered
    assert '- planning.Locks: "Harvest home-grown greens" -> "Use shop-bought whole scallion"' in rendered
    assert "Problem:" not in rendered
    assert rendered.index("Governed changes") < rendered.index("Complete linked candidate change set")
    assert rendered.index("Complete linked candidate change set") < rendered.index("Approve: dish-admin review-approve")
    assert f"Reject template: dish-admin review-reject {proposal_id}" in rendered
    assert f"Reject: dish-admin review-reject {proposal_id}" not in rendered

    verbose = render_admin_result(result, profile="prod", verbose=True)
    assert "Problem: The title still requires home-grown scallion greens." in verbose
    assert verbose.count("Complete linked candidate change set") == 1
    assert verbose.index("Complete linked candidate change set") < verbose.index("Approve: dish-admin review-approve")
