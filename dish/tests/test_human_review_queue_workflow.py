from __future__ import annotations

import pytest

from dish_tool.admin import DishAdminApplication
from tests.support.verification import TASK, make_app, review_and_inspect


@pytest.mark.smoke
def test_human_review_hold_appears_in_review_queue_and_can_be_resolved_by_number(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="human-review-author")
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="Marco must choose whether this is a tasting portion or a complete main meal.",
        resume_status="pending-verification",
        run_id="human-review-author",
    )
    assert held["ok"]
    assert held["data"]["batch_may_continue"] is True

    admin = DishAdminApplication(
        app.conn,
        backend=backend,
        release_loader=lambda: app._load_release("verification"),
    )
    queue = admin.execute("review-queue", status="pending")
    assert queue["ok"] and queue["data"]["count"] == 1
    item = queue["data"]["review_items"][0]
    assert item["item_type"] == "human_review"
    assert item["operation_id"] == operation_id

    inspected = admin.execute("review-inspect", proposal_id="1")
    assert inspected["ok"]
    assert inspected["data"]["review_item"]["review_id"] == item["review_id"]
    assert inspected["data"]["admin_command"].startswith(
        f"dish-admin record-human-decision {operation_id}"
    )

    missing = admin.execute("review-approve", proposal_id="1", reason="approved")
    assert missing["code"] == "INVALID_ARGUMENT"
    assert missing["errors"][0]["rule"] == "human_review_detail_required"

    resolved = admin.execute(
        "review-approve",
        proposal_id="1",
        reason="approved",
        detail="Marco confirms this is a non-main tasting portion.",
    )
    assert resolved["ok"]
    assert "Status: pending-verification" in backend.notes
    assert admin.execute("review-queue", status="pending")["data"]["count"] == 0


@pytest.mark.smoke
def test_service_review_queue_resolves_human_hold_by_current_row_number(tmp_path):
    from dish_service.leases import ServicePrincipal
    from tests.support.service_leases import _service

    backend = __import__("tests.support.verification", fromlist=["Backend"]).Backend()
    service = _service(tmp_path, backend)
    constructor = ServicePrincipal(owner_id="constructor", run_id="constructor-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
    )
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]

    verifier = ServicePrincipal(owner_id="verifier", run_id="verifier-run")
    verification = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=verifier,
    )
    assert verification["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": started["submission_id"]},
        principal=verifier,
    )
    assert inspected["ok"]
    held = service.execute_agent(
        "reject",
        {
            "agent": "codex",
            "submission_id": started["submission_id"],
            "route": "human-review",
            "reason": "Marco must choose whether this is a tasting portion or a complete meal.",
            "resume_status": "pending-verification",
        },
        principal=verifier,
    )
    assert held["ok"] and held["data"]["batch_may_continue"] is True

    marco = ServicePrincipal(owner_id="marco", run_id="marco-review")
    queue = service.execute_admin("review-queue", {}, principal=marco)
    assert queue["ok"]
    assert queue["data"]["review_items"][0]["item_type"] == "human_review"
    resolved = service.execute_admin(
        "review-approve",
        {
            "proposal_id": "1",
            "reason": "Marco decided after reviewing the hold.",
            "detail": "Marco confirms this is a non-main tasting portion.",
        },
        principal=marco,
    )
    assert resolved["ok"]
    assert "Status: pending-verification" in backend.notes
