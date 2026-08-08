from __future__ import annotations

import uuid

from dish_service.admin_cli import build_parser
from dish_tool.admin_human import render_admin_result


def test_review_approve_parser_accepts_human_review_detail_and_queue_number():
    parsed = build_parser().parse_args(
        [
            "review-approve",
            "1",
            "--detail",
            "Marco chooses the smaller tasting format.",
        ]
    )

    assert parsed.proposal_id == "1"
    assert parsed.detail == "Marco chooses the smaller tasting format."


def test_review_queue_renderer_prints_exact_commands_and_explains_row_numbers():
    proposal_id = str(uuid.uuid4())
    result = {
        "ok": True,
        "command": "review-queue",
        "code": "OK",
        "state": "ok",
        "retryable": False,
        "allowed_actions": [],
        "data": {
            "review_items": [
                {
                    "item_type": "semantic_proposal",
                    "review_id": proposal_id,
                    "proposal_id": proposal_id,
                    "status": "pending",
                    "candidate_title": "Vietnamese scallion egg",
                    "proposal_reason": "Remove the stale harvest dependency.",
                    "changes": [{"field": "Dish candidate"}],
                }
            ]
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert f"Inspect: dish-admin review-inspect {proposal_id}" in rendered
    assert f"Approve: dish-admin review-approve {proposal_id}" not in rendered
    assert f"Reject: dish-admin review-reject {proposal_id}" in rendered
    assert "Queue numbers are accepted only for the current queue view" in rendered
