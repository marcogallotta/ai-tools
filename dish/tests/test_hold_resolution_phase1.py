from __future__ import annotations

import pytest

from dish_service.command_spec import action_openapi_argument_schema
from dish_tool.admin_cli import build_parser
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
