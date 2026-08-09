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
        human_review_confirmed=True,
        human_review_basis="Only Marco can resolve the remaining choice within settled authority.",
        repairs_considered="Plausible within-authority repairs were considered and do not resolve the choice.",
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
    missing_decision = admin.execute("review-approve", proposal_id="1")
    assert missing_decision["ok"] is False
    assert missing_decision["code"] == "INVALID_ARGUMENT"
    assert missing_decision["errors"][0]["rule"] == "human_review_detail_required"
    assert inspected["data"]["admin_command"].startswith(
        f"dish-admin review-approve {item['review_id']}"
    )
    assert inspected["data"]["review_item"]["review_summary"]["outcome"] == "needs Marco decision"
    assert "Only Marco can resolve" in inspected["data"]["review_item"]["review_summary"]["decision"]
    approval = inspected["data"]["human_action"]
    assert approval["command"] == "review-approve"
    assert approval["after_success"]["resume_status"] == "pending-verification"
    assert approval["after_success"]["next_stage"] == "verification"
    dismissal = next(action for action in inspected["data"]["human_actions"] if action["command"] == "review-reject")
    assert dismissal["after_success"]["resume_status"] == "pending-verification"
    assert dismissal["after_success"]["next_stage"] == "verification"

    resolved = admin.execute(
        "review-approve",
        proposal_id="1",
        reason="Marco confirms this is a non-main tasting portion.",
    )
    assert resolved["ok"]
    assert resolved["command"] == "review-approve"
    assert resolved["data"]["resume_status"] == "pending-verification"
    assert resolved["data"]["approval_consequence"]["next_stage"] == "verification"
    assert "Status: pending-verification" in backend.notes
    assert admin.execute("review-queue", status="pending")["data"]["count"] == 0


@pytest.mark.smoke
def test_review_approve_human_review_rejects_legacy_semantic_default_reason(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="legacy-default")
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="human-review",
        reason="Marco must choose.", resume_status="pending-verification",
        human_review_confirmed=True, human_review_basis="Only Marco can choose.",
        repairs_considered="No exact governed repair exists before Marco chooses.", run_id="legacy-default",
    )
    assert held["ok"]
    cycle_id = app.conn.execute(
        "SELECT cycle_id FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()[0]
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release("verification"))
    result = admin.execute(
        "review-approve", proposal_id=cycle_id,
        reason="Approved after reviewing the exact linked change bundle.",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "human_review_detail_required"


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
            "human_review_confirmed": True,
            "human_review_basis": "Only Marco can resolve the remaining choice within settled authority.",
            "repairs_considered": "Plausible within-authority repairs were considered and do not resolve the choice.",
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
            "reason": "Marco confirms this is a non-main tasting portion.",
        },
        principal=marco,
    )
    assert resolved["ok"]
    assert resolved["command"] == "review-approve"
    assert "Status: pending-verification" in backend.notes

    # A deliberate non-material Human Review pause is resumable by the same live
    # verifier run.  It is a new cycle, not a reason to fabricate a new agent.
    resumed = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent",
        },
        principal=verifier,
    )
    assert resumed["ok"]
    assert resumed["allowed_actions"] == ["inspect"]
    from dish_tool.database_initialization import initialize_database

    conn = initialize_database(service.config.db_path)
    try:
        verifier_facts = conn.execute(
            """SELECT source_cycle_id,candidate_identity FROM operation_actor_facts
                 WHERE operation_id=? AND role='verifier' AND run_id=?
                 ORDER BY created_at,fact_id""",
            (started["submission_id"], "verifier-run"),
        ).fetchall()
        assert len(verifier_facts) == 2
        assert verifier_facts[0]["source_cycle_id"] != verifier_facts[1]["source_cycle_id"]
    finally:
        conn.close()


@pytest.mark.smoke
def test_review_reject_dismisses_unanswered_human_review_without_recording_decision(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="bad-review")
    before_decisions = tuple(__import__("dish_tool.task_document", fromlist=["parse_task_document"]).parse_task_document(
        f"{backend.title}\n{backend.notes}"
    ).decisions)
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="51 g fat per portion based on raw whole-bird extrapolation.",
        resume_status="pending-verification",
        run_id="bad-review",
        human_review_confirmed=True,
        human_review_basis="The estimate appears to require a nutrition exemption.",
        repairs_considered="No repair was accepted before escalation.",
        blocker_metric="fat",
        blocker_actual=51,
        blocker_limit=40,
        blocker_delta=11,
        blocker_unit="g",
        blocker_basis="served-edible estimate",
    )
    assert held["ok"]
    cycle_id = app.conn.execute(
        "SELECT cycle_id FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()[0]

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release("verification")
    )
    queue = admin.execute("review-queue", status="pending")
    summary = queue["data"]["review_items"][0]["review_summary"]
    assert summary["outcome"] == "needs Marco decision"
    assert summary["decision"] == "The estimate appears to require a nutrition exemption."
    assert summary["quantified_blocker"] == "fat: 51 g vs 40 g (+11 g)"

    inspected = admin.execute("inspect", submission_id=operation_id)
    kinds = [item["kind"] for item in inspected["data"]["human_actions"]]
    assert kinds == ["review-human-decision", "dismiss-human-review"]
    dismiss_action = next(action for action in inspected["data"]["human_actions"] if action["command"] == "review-reject")
    assert dismiss_action["after_success"]["resume_status"] == "pending-verification"
    assert dismiss_action["after_success"]["next_stage"] == "verification"

    dismissed = admin.execute(
        "review-reject",
        proposal_id=cycle_id,
        reason="The 51 g estimate incorrectly treated raw whole-bird fat as served fat and is not a defensible blocker.",
    )
    assert dismissed["ok"]
    assert dismissed["command"] == "review-reject"
    assert dismissed["data"]["resolution_mode"] == "dismissal"
    assert dismissed["data"]["new_cycle_id"]
    after_doc = __import__("dish_tool.task_document", fromlist=["parse_task_document"]).parse_task_document(
        f"{backend.title}\n{backend.notes}"
    )
    assert tuple(after_doc.decisions) == before_decisions
    assert after_doc.state.values["Status"] == "pending-verification"
    audit = app.conn.execute(
        "SELECT event_type,details,governed_kind,json_extract(actor_provenance,'$.source') AS actor_source FROM audit_events WHERE operation_id=? AND event_type='human_review.dismissed' ORDER BY created_at DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert audit is not None
    assert audit["event_type"] == "human_review.dismissed"
    assert audit["governed_kind"] is None
    assert audit["actor_source"] == "human-review-dismissal"

    next_review = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="verification",
        run_id="fresh-after-dismissal",
        independence_attestation="independent",
    )
    assert next_review["ok"]
    context = next_review["data"]["dismissed_human_review"]
    assert context[-1]["original_issue"].startswith("51 g fat")
    assert "raw whole-bird fat" in context[-1]["dismissal_reason"]
    assert "Do not carry its premise forward" in context[-1]["instruction"]

@pytest.mark.smoke
def test_review_approve_pending_research_human_review_advertises_and_returns_research(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="research-route-review")
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="Marco must decide whether this concern should return to Research.",
        resume_status="pending-research",
        run_id="research-route-review",
        human_review_confirmed=True,
        human_review_basis="Only Marco can decide whether the research premise should change.",
        repairs_considered="Verification cannot construct the exact research change without Marco's choice.",
    )
    assert held["ok"]
    cycle_id = app.conn.execute(
        "SELECT cycle_id FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()[0]

    # Agent-facing continuation must distinguish approval (Research) from dismissal (Verification).
    assert held["data"]["after_resolution"]["approval"]["resume_status"] == "pending-research"
    assert held["data"]["after_resolution"]["approval"]["next_stage"] == "research"
    assert held["data"]["after_resolution"]["dismissal"]["resume_status"] == "pending-verification"
    assert held["data"]["after_resolution"]["dismissal"]["next_stage"] == "verification"

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release("verification")
    )
    inspected = admin.execute("review-inspect", proposal_id=cycle_id)
    assert inspected["ok"]
    assert inspected["data"]["human_action"]["after_success"]["next_stage"] == "research"

    resolved = admin.execute(
        "review-approve",
        proposal_id=cycle_id,
        reason="Marco requires another Research pass before Verification.",
    )
    assert resolved["ok"]
    assert resolved["command"] == "review-approve"
    assert resolved["data"]["resume_status"] == "pending-research"
    assert resolved["data"]["new_cycle_id"] is None
    assert resolved["data"]["approval_consequence"]["next_stage"] == "research"
    assert "returned to Research" in resolved["data"]["effect"]

    operation = app.conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    assert dict(operation) == {
        "status": "completed",
        "phase": "terminal",
        "terminal_outcome": "human_review_resolved_to_research",
    }
    doc = __import__("dish_tool.task_document", fromlist=["parse_task_document"]).parse_task_document(
        f"{backend.title}\n{backend.notes}"
    )
    assert doc.state.values["Status"] == "pending-research"
    assert doc.state.values["Resume status"] == "None"


@pytest.mark.smoke
def test_review_reject_pending_research_human_review_forces_fresh_verification(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="bad-review-research")
    parse_task_document = __import__(
        "dish_tool.task_document", fromlist=["parse_task_document"]
    ).parse_task_document
    before_doc = parse_task_document(f"{backend.title}\n{backend.notes}")
    before_decisions = tuple(before_doc.decisions)
    authorizations_before = app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0]

    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="The nutrition estimate may require more Research before Verification can continue.",
        resume_status="pending-research",
        run_id="bad-review-research",
        human_review_confirmed=True,
        human_review_basis="The verifier treated the estimate as needing a Marco-only route choice.",
        repairs_considered="The verifier did not establish a defensible repair before escalating.",
    )
    assert held["ok"]
    source_cycle_id = app.conn.execute(
        "SELECT cycle_id FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()[0]

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release("verification")
    )
    review = admin.execute("review-inspect", proposal_id=source_cycle_id)
    assert review["ok"]
    approval = review["data"]["human_action"]
    assert approval["after_success"]["resume_status"] == "pending-research"
    assert approval["after_success"]["next_stage"] == "research"
    dismissal = next(action for action in review["data"]["human_actions"] if action["command"] == "review-reject")
    assert dismissal["after_success"]["resume_status"] == "pending-verification"
    assert dismissal["after_success"]["next_stage"] == "verification"

    dismissed = admin.execute(
        "review-reject",
        proposal_id=source_cycle_id,
        reason="The escalation was invalid: its premise needs fresh Verification, not a Marco routing decision.",
    )
    assert dismissed["ok"]
    assert dismissed["command"] == "review-reject"
    assert dismissed["data"]["resolution_mode"] == "dismissal"
    assert dismissed["data"]["resume_status"] == "pending-verification"
    assert dismissed["data"]["new_cycle_id"]

    after_doc = parse_task_document(f"{backend.title}\n{backend.notes}")
    assert after_doc.state.values["Status"] == "pending-verification"
    assert after_doc.state.values["Resume status"] == "None"
    assert tuple(after_doc.decisions) == before_decisions

    operation = app.conn.execute(
        "SELECT status,phase FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    assert dict(operation) == {"status": "open", "phase": "await_verification"}
    new_cycle = app.conn.execute(
        "SELECT cycle_id,route,outcome FROM verification_cycles WHERE cycle_id=?",
        (dismissed["data"]["new_cycle_id"],),
    ).fetchone()
    assert new_cycle is not None
    assert new_cycle["cycle_id"] != source_cycle_id
    assert new_cycle["route"] is None
    assert new_cycle["outcome"] is None

    authorizations_after = app.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0]
    assert authorizations_after == authorizations_before
    audit = app.conn.execute(
        "SELECT event_type,details,governed_kind,json_extract(actor_provenance,'$.source') AS actor_source FROM audit_events "
        "WHERE operation_id=? AND event_type='human_review.dismissed' "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert audit is not None
    assert audit["event_type"] == "human_review.dismissed"
    assert audit["governed_kind"] is None
    assert audit["actor_source"] == "human-review-dismissal"

    fresh = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="verification",
        run_id="fresh-after-research-dismissal",
        independence_attestation="independent",
    )
    assert fresh["ok"]
    context = fresh["data"]["dismissed_human_review"]
    assert context[-1]["source_cycle_id"] == source_cycle_id
    assert "nutrition estimate" in context[-1]["original_issue"]
    assert "premise needs fresh Verification" in context[-1]["dismissal_reason"]
    assert "reassess" in context[-1]["instruction"]
