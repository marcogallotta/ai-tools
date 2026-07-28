from __future__ import annotations

import json

import pytest

from test_dish_tool_r27_r29_readiness import _approve_and_submit
from test_dish_tool_step7_verification import TASK, make_app


ATTESTATION = "independent verifier run"


@pytest.mark.parametrize("attestation", ["", "   "])
def test_verification_start_rejects_blank_attestation_before_mutation(
    tmp_path, attestation
):
    app, backend, operation_id, _ = make_app(tmp_path)
    writes = backend.writes

    result = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="blank-attestation",
        independence_attestation=attestation,
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is True
    assert result["errors"] == [
        {
            "rule": "independence_attestation_required",
            "field": "independence_attestation",
        }
    ]
    cycle = app.conn.execute(
        "SELECT verifier_agent,run_id,independence_attestation,reviewed_identity "
        "FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()
    assert tuple(cycle) == (None, None, None, None)
    assert backend.writes == writes
    assert app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE operation_id=? "
        "AND event_type='verification.review_started'",
        (operation_id,),
    ).fetchone()[0] == 0


def test_approve_rejects_blank_attestation_then_accepts_corrected_call(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="approve-attestation",
        independence_attestation=ATTESTATION,
    )
    assert app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )["ok"]

    rejected = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="approve-attestation",
        independence_attestation=" ",
    )
    assert rejected["code"] == "INVALID_ARGUMENT"
    assert rejected["retryable"] is True
    assert rejected["errors"][0] == {
        "rule": "independence_attestation_required",
        "field": "independence_attestation",
    }

    corrected = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="approve-attestation",
        independence_attestation=ATTESTATION,
    )
    assert corrected["ok"]


def test_large_reject_rejects_blank_attestation_then_accepts_corrected_call(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="reject-attestation",
        independence_attestation=ATTESTATION,
    )
    assert app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )["ok"]
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK)

    rejected = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="method needs replacement",
        file_path=str(candidate),
        run_id="reject-attestation",
        independence_attestation="",
    )
    assert rejected["code"] == "INVALID_ARGUMENT"
    assert rejected["retryable"] is True
    assert rejected["errors"][0] == {
        "rule": "independence_attestation_required",
        "field": "independence_attestation",
    }

    corrected = app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="method needs replacement",
        file_path=str(candidate),
        run_id="reject-attestation",
        independence_attestation=ATTESTATION,
    )
    assert corrected["ok"]


@pytest.mark.parametrize("route", ["evidence", "human-review"])
def test_hold_rejection_inherits_persisted_attestation(route, tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="hold-attestation",
        independence_attestation=ATTESTATION,
    )
    assert app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )["ok"]

    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route=route,
        reason="Marco must resolve the blocker",
        resume_status="pending-verification",
        run_id="hold-attestation",
    )
    assert held["ok"]
    audit = app.conn.execute(
        "SELECT actor_provenance FROM audit_events WHERE operation_id=? "
        "AND event_type='verification.rejected' ORDER BY rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert json.loads(audit["actor_provenance"])["independence_attestation"] == ATTESTATION


def test_quantity_forced_verification_is_audited_large_through_approval(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id, run="initial-review")
    changed = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="adjust the quantity",
        run_id="quantity-editor",
    )
    candidate = tmp_path / "quantity.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "100 g test ingredient", "110 g test ingredient"
        )
    )

    prepared = app.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=changed["submission_id"],
        file_path=str(candidate),
        material_classification="non-material",
        run_id="quantity-editor",
    )
    assert prepared["ok"]
    assert prepared["data"]["material_classification"]["forced_material_reasons"] == [
        "quantities",
        "quantity",
        "portions",
    ]
    pending_line = next(
        line for line in backend.notes.splitlines() if "updated the candidate" in line
    )
    assert " — Large — pending-verification" in pending_line

    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="quantity-review",
        independence_attestation=ATTESTATION,
    )
    assert app.execute(
        "inspect", agent="codex", submission_id=changed["submission_id"]
    )["ok"]
    approved = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=changed["submission_id"],
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="quantity-review",
        independence_attestation=ATTESTATION,
    )
    assert approved["ok"]
    verified_line = next(
        line for line in backend.notes.splitlines() if "updated the candidate" in line
    )
    assert " — Large — verified — " in verified_line


@pytest.mark.parametrize(
    ("route", "phase", "admin_action"),
    [
        ("evidence", "held_evidence", "supply-evidence"),
        ("human-review", "held_human", "record-human-decision"),
    ],
)
def test_blocked_start_preserves_held_operation_guidance(
    tmp_path, route, phase, admin_action
):
    app, _backend, operation_id, _ = make_app(tmp_path)
    app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="hold-review",
        independence_attestation=ATTESTATION,
    )
    assert app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )["ok"]
    held = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route=route,
        reason="Marco must resolve the blocker",
        resume_status="pending-verification",
        run_id="hold-review",
    )
    assert held["ok"]

    for _ in range(2):
        blocked = app.execute(
            "start",
            agent="gpt",
            task_gid="t",
            kind="change",
            change_level="small",
            change_reason="attempt a new change",
            run_id="blocked-editor",
        )
        assert blocked["code"] == "CONFLICT"
        assert blocked["submission_id"] == operation_id
        assert blocked["data"] == {
            "message": "task already has an open operation",
            "existing_submission_id": operation_id,
            "phase": phase,
            "required_admin_action": admin_action,
            "resolver": f"Marco/admin {admin_action}",
        }


def test_missing_material_classification_is_retryable_on_same_operation(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id, run="classification-baseline")
    changed = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="clarify the method",
        run_id="classification-editor",
    )
    candidate = tmp_path / "classification.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace("1. Cook it.", "1. Cook it gently.")
    )

    missing = app.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=changed["submission_id"],
        file_path=str(candidate),
        run_id="classification-editor",
    )
    assert missing["code"] == "INVALID_ARGUMENT"
    assert missing["errors"][0]["rule"] == "material_classification_required"
    assert missing["retryable"] is True
    assert missing["allowed_actions"] == ["prepare"]

    corrected = app.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=changed["submission_id"],
        file_path=str(candidate),
        material_classification="material",
        run_id="classification-editor",
    )
    assert corrected["ok"]
    assert corrected["submission_id"] == changed["submission_id"]


def test_hold_route_argument_error_is_retryable_and_diagnostic_uses_action_fields(
    tmp_path,
):
    app, _backend, operation_id, _ = make_app(tmp_path)
    app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="route-review",
        independence_attestation=ATTESTATION,
    )
    assert app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )["ok"]
    candidate = tmp_path / "not-accepted.txt"
    candidate.write_text(TASK)

    invalid = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="evidence",
        reason="Marco must confirm a fact",
        file_path=str(candidate),
        model="gpt-5.6-sol",
        resume_status=None,
        run_id="route-review",
    )
    assert invalid["code"] == "INVALID_ARGUMENT"
    assert invalid["retryable"] is True
    overall = next(
        error
        for error in invalid["errors"]
        if error["rule"] == "rejection_route_arguments_invalid"
    )
    assert overall["permitted_arguments"] == [
        "submission_id",
        "agent",
        "reason",
        "route",
        "resume_status",
    ]

    corrected = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="evidence",
        reason="Marco must confirm a fact",
        resume_status="pending-verification",
        run_id="route-review",
    )
    assert corrected["ok"]
