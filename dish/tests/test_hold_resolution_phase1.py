from __future__ import annotations

import pytest

from dish_service.command_spec import action_openapi_argument_schema
from dish_service.admin_cli import build_parser
from dish_tool.errors import DishRuleError
from dish_tool.step8 import _validated_quantified_blocker


def test_holds_cli_is_read_only_and_resolution_guards_are_required():
    assert build_parser().parse_args(["holds"]).command == "holds"
    parsed = build_parser().parse_args([
        "supply-evidence", "11111111-1111-1111-1111-111111111111",
        "--detail", "source confirms the value",
        "--resume-status", "pending-verification",
        "--expected-task-gid", "123",
        "--expected-cycle-id", "22222222-2222-2222-2222-222222222222",
        "--expected-hold-identity", "abc",
    ])
    assert parsed.expected_task_gid == "123"


def test_quantified_blocker_is_complete_and_arithmetic_is_checked():
    assert _validated_quantified_blocker(metric="fat", actual=52, limit=40, delta=12, unit="g", basis="ingredient calculation") == {
        "metric": "fat", "actual": 52.0, "limit": 40.0, "delta": 12.0, "unit": "g", "basis": "ingredient calculation"
    }
    with pytest.raises(DishRuleError) as incomplete:
        _validated_quantified_blocker(metric="fat", actual=52)
    assert incomplete.value.rule == "quantified_blocker_incomplete"
    with pytest.raises(DishRuleError) as mismatch:
        _validated_quantified_blocker(metric="fat", actual=52, limit=40, delta=10, unit="g", basis="calculation")
    assert mismatch.value.rule == "quantified_blocker_delta_mismatch"


def test_reject_openapi_exposes_optional_quantified_blocker_fields_on_hold_routes():
    schema = action_openapi_argument_schema("reject")
    variants = {variant["properties"]["route"]["const"]: variant for variant in schema["oneOf"]}
    for route in ("evidence", "human-review"):
        properties = variants[route]["properties"]
        for name in ("blocker_metric", "blocker_actual", "blocker_limit", "blocker_delta", "blocker_unit", "blocker_basis"):
            assert name in properties
    assert "blocker_metric" not in variants["large"]["properties"]


def test_holds_lists_verification_evidence_with_exact_resolution_binding(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from tests.support.verification import make_app, review_and_inspect

    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app)
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="evidence",
        reason="Quantified source check required",
        resume_status="pending-verification",
        run_id="review",
        blocker_metric="fat",
        blocker_actual=52,
        blocker_limit=40,
        blocker_delta=12,
        blocker_unit="g",
        blocker_basis="ingredient calculation",
    )
    assert held["ok"]

    admin = DishAdminApplication(
        app.conn,
        backend=backend,
        release_loader=lambda: app._load_release("verification"),
    )
    listed = admin.execute("holds")
    assert listed["ok"]
    assert listed["data"]["count"] == 1
    hold = listed["data"]["holds"][0]
    assert hold["hold_class"] == "verification_evidence"
    assert hold["required_admin_action"] == "supply-evidence"
    assert hold["task_gid"] == "t"
    assert hold["operation_id"] == operation_id
    assert hold["question"] == "Quantified source check required"
    assert hold["cycle_id"]
    assert len(hold["hold_identity"]) == 64
    assert hold["asana_url"].endswith("/t")


def test_current_verification_question_wins_over_historical_preconstruction_hold(tmp_path):
    from dish_tool.admin import _durable_hold_question
    from dish_tool.database import complete_operation_step, declare_operation_step
    from tests.support.verification import make_app, review_and_inspect

    app, _, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app)
    declare_operation_step(
        app.conn,
        operation_id,
        "research_preconstruction_hold",
        {
            "route": "evidence",
            "reason": "Historical preconstruction question",
            "resume_status": "pending-research",
        },
    )
    complete_operation_step(app.conn, operation_id, "research_preconstruction_hold")
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="evidence",
        reason="Current verification question",
        resume_status="pending-verification",
        run_id="review",
    )
    assert held["ok"]

    assert _durable_hold_question(app.conn, operation_id) == "Current verification question"


def test_resolution_rejects_mismatched_stable_hold_binding(tmp_path):
    from dish_tool.admin import DishAdminApplication
    from tests.support.verification import make_app, review_and_inspect

    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app)
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="Marco must decide",
        resume_status="pending-verification",
        human_review_confirmed=True,
        human_review_basis="Only Marco can resolve the remaining choice within settled authority.",
        repairs_considered="Plausible within-authority repairs were considered and do not resolve the choice.",
        human_review_options=[{"label": "Use Marco's decision", "decision": "Apply Marco's chosen resolution."}],
        run_id="review",
    )
    assert held["ok"]
    cycle = app.conn.execute(
        "SELECT cycle_id, hold_identity FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    admin = DishAdminApplication(
        app.conn,
        backend=backend,
        release_loader=lambda: app._load_release("verification"),
    )

    wrong_task = admin.execute(
        "record-human-decision",
        submission_id=operation_id,
        detail="Marco decided",
        resume_status="pending-verification",
        expected_task_gid="wrong-task",
        expected_cycle_id=cycle["cycle_id"],
        expected_hold_identity=cycle["hold_identity"],
    )
    assert wrong_task["code"] == "CONFLICT"
    assert wrong_task["errors"][0]["rule"] == "hold_task_mismatch"

    wrong_cycle = admin.execute(
        "record-human-decision",
        submission_id=operation_id,
        detail="Marco decided",
        resume_status="pending-verification",
        expected_task_gid="t",
        expected_cycle_id="00000000-0000-0000-0000-000000000000",
        expected_hold_identity=cycle["hold_identity"],
    )
    assert wrong_cycle["code"] == "CONFLICT"
    assert wrong_cycle["errors"][0]["rule"] == "hold_cycle_mismatch"

    wrong_identity = admin.execute(
        "record-human-decision",
        submission_id=operation_id,
        detail="Marco decided",
        resume_status="pending-verification",
        expected_task_gid="t",
        expected_cycle_id=cycle["cycle_id"],
        expected_hold_identity="0" * 64,
    )
    assert wrong_identity["code"] == "CONFLICT"
    assert wrong_identity["errors"][0]["rule"] == "hold_identity_mismatch"


def test_human_review_requires_neutral_escalation_preflight_before_hold(tmp_path):
    from tests.support.verification import make_app, review_and_inspect

    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app)
    first = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="Estimated fat may exceed the protocol ceiling.",
        resume_status="pending-verification",
        run_id="review",
        blocker_metric="fat",
        blocker_actual=51,
        blocker_limit=40,
        blocker_delta=11,
        blocker_unit="g",
        blocker_basis="rough comparison-recipe extrapolation",
    )
    assert first["code"] == "CONFIRMATION_REQUIRED"
    assert first["errors"][0]["rule"] == "human_review_preflight_required"
    details = first["errors"][0]
    assert details["human_review_is_allowed"].startswith("Human Review is appropriate")
    assert "reasonable defensible estimate" in details["decision_standard"]
    assert "must state one defensible estimate" in details["decision_standard"]
    assert "range" not in details["decision_standard"].lower()
    assert "Large correction" in details["exact_resolution_route"]
    assert any("exact governed fix" in question for question in details["questions"])
    assert details["retry"]["human_review_confirmed"] is True
    assert app.conn.execute(
        "SELECT phase FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == "await_verification"
    assert "Status: pending-verification" in backend.notes

    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="human-review",
        reason="Estimated fat may exceed the protocol ceiling.",
        resume_status="pending-verification",
        run_id="review",
        blocker_metric="fat",
        blocker_actual=51,
        blocker_limit=40,
        blocker_delta=11,
        blocker_unit="g",
        blocker_basis="validated served-edible calculation",
        human_review_confirmed=True,
        human_review_basis="Meeting the ceiling would require Marco to change the settled construction or grant an exemption.",
        repairs_considered="Recalculated served edible fat and tested lower retained oil; neither resolves the ceiling without changing a settled lock.",
        human_review_options=[
            {"label": "Approve an exception", "decision": "Approve an exception to the nutrition ceiling for this dish."},
            {"label": "Change the settled construction", "decision": "Change the settled construction to meet the ceiling."},
        ],
    )
    assert held["ok"]
    assert "Status: pending-human-review" in backend.notes


def test_small_incidental_governed_text_edit_is_challenged_before_proposal(tmp_path):
    from tests.support.verification import TASK, make_app, review_and_inspect

    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="review")
    candidate = tmp_path / "incidental-governed.txt"
    candidate.write_text(
        TASK.replace("100 g test ingredient", "120 g test ingredient")
        .replace("Purpose: Compare texture", "Purpose: Comparé texture")
    )

    first = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Correct the ingredient quantity.",
        file_path=str(candidate),
        run_id="review",
    )
    assert first["code"] == "CONFIRMATION_REQUIRED"
    assert first["errors"][0]["rule"] == "governed_change_intent_confirmation_required"
    flagged = first["errors"][0]["governed_changes_needing_confirmation"]
    assert [item["field"] for item in flagged] == ["Purpose"]
    assert app.conn.execute("SELECT COUNT(*) FROM semantic_proposals").fetchone()[0] == 0
    assert "120 g" not in backend.notes

    corrected = tmp_path / "corrected.txt"
    corrected.write_text(TASK.replace("100 g test ingredient", "120 g test ingredient"))
    applied = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Correct the ingredient quantity.",
        file_path=str(corrected),
        run_id="review",
    )
    assert applied["ok"]
    assert "120 g test ingredient" in backend.notes
    assert "Purpose: Compare texture" in backend.notes
    assert app.conn.execute("SELECT COUNT(*) FROM semantic_proposals").fetchone()[0] == 0


def test_intentional_small_governed_text_edit_can_be_explicitly_confirmed(tmp_path):
    from tests.support.verification import TASK, make_app, review_and_inspect

    app, _backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="review")
    candidate = tmp_path / "intentional-governed.txt"
    candidate.write_text(TASK.replace("Purpose: Compare texture", "Purpose: Comparé texture"))

    queued = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="Marco intentionally changed this exact Purpose wording.",
        file_path=str(candidate),
        run_id="review",
        governed_change_fields=["Purpose"],
    )
    assert queued["code"] == "VALIDATION_FAILED"
    assert queued["errors"][0]["rule"] == "semantic_proposal_queued"


def test_service_keeps_no_effect_governed_intent_challenge_as_confirmation_required(tmp_path):
    import uuid

    from dish_service.leases import ServicePrincipal
    from tests.support.service_leases import _service
    from tests.support.verification import Backend, TASK

    backend = Backend()
    service = _service(tmp_path, backend)
    constructor = ServicePrincipal(owner_id="constructor", run_id="constructor-run")
    started = service.execute_agent(
        "start", {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor, request_id=str(uuid.uuid4()),
    )
    assert service.execute_agent(
        "prepare", {
            "agent": "gpt", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "file_text": TASK,
        }, principal=constructor, request_id=str(uuid.uuid4()),
    )["ok"]
    verifier = ServicePrincipal(owner_id="verifier", run_id="verification-run")
    assert service.execute_agent(
        "start", {
            "agent": "codex", "task_gid": "t", "kind": "verification",
            "independence_attestation": "independent",
        }, principal=verifier, request_id=str(uuid.uuid4()),
    )["ok"]
    assert service.execute_agent(
        "inspect", {"agent": "codex", "submission_id": started["submission_id"]},
        principal=verifier, request_id=str(uuid.uuid4()),
    )["ok"]

    candidate = (
        TASK.replace("100 g test ingredient", "120 g test ingredient")
        .replace("Purpose: Compare texture", "Purpose: Comparé texture")
    )
    first_request = str(uuid.uuid4())
    challenged = service.execute_agent(
        "reject", {
            "agent": "codex", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "route": "large",
            "reason": "Correct the ingredient quantity.", "file_text": candidate,
        }, principal=verifier, request_id=first_request,
    )
    assert challenged["code"] == "CONFIRMATION_REQUIRED"
    assert challenged["errors"][0]["rule"] == "governed_change_intent_confirmation_required"
    assert challenged["errors"][0]["fresh_request_id"] is True
    assert challenged["code"] != "BACKEND_UNCERTAIN"
    assert backend.writes == 1  # constructor write only; the challenged correction wrote nothing

    corrected = TASK.replace("100 g test ingredient", "120 g test ingredient")
    retried = service.execute_agent(
        "reject", {
            "agent": "codex", "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"], "route": "large",
            "reason": "Correct the ingredient quantity.", "file_text": corrected,
        }, principal=verifier, request_id=str(uuid.uuid4()),
    )
    assert retried["ok"]
    assert "120 g test ingredient" in backend.notes
