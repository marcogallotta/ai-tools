import dataclasses

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.database import reserve_marco_authorizations
from dish_tool.errors import DishRuleError
from dish_tool.governed_diff import explicit_material_reasons, require_small_scope
from dish_tool.task_document import parse_task_document
from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
from tests.test_dish_tool_step7_verification import TASK, make_app


def _doc(text=TASK):
    return parse_task_document(text)


def _review(app, *, run="review", agent="codex"):
    result = app.execute("start", agent=agent, task_gid="t", kind="verification", run_id=run)
    assert result["ok"]
    return result


@pytest.mark.parametrize(
    "mutator,expected_reason",
    [
        (lambda text: text.replace("Dish candidate: Test dish", "Dish candidate: Different dish"), "dish_candidate"),
        (lambda text: text.replace("Compare hydration routes.", "Maximise sweetness."), "purpose_or_test"),
        (lambda text: text.replace("Crisp and aromatic.", "Soft and mild."), "success_criteria"),
        (lambda text: text.replace("None - pantry snapshot lists required items in stock", "Fresh lamb shoulder"), "ingredient_identity"),
        (lambda text: text.replace("1. Cook it.", "1. Pork is permitted; cook it."), "method"),
        (lambda text: text.replace("Destination section: Sichuan — 12345", "Destination section: Planned — 999"), "destination"),
    ],
)
def test_canonical_material_fields_force_fresh_verification(mutator, expected_reason):
    before = _doc()
    after = _doc(mutator(TASK))
    reasons = explicit_material_reasons(before, after)
    assert expected_reason in reasons
    with pytest.raises(DishRuleError) as exc:
        require_small_scope(before, after)
    assert exc.value.rule == "large_correction_required"


@pytest.mark.parametrize(
    "replacement",
    [
        "1. Cook it gently.",
        "1. Cook it.\n2. Keep the crisp component separate until serving.",
        "1. Cook it.\n2. Finish freshly just before serving.",
    ],
)
def test_handling_only_method_changes_remain_small_capable(replacement):
    before = _doc()
    after = _doc(TASK.replace("1. Cook it.", replacement))
    assert explicit_material_reasons(before, after) == ()
    require_small_scope(before, after)


def test_real_small_route_rejects_same_keyword_halal_reversal(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = _review(app)
    candidate = tmp_path / "small-halal.txt"
    candidate.write_text(TASK.replace("1. Cook it.", "1. Pork is permitted; cook it."))
    result = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="small", file_path=str(candidate),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "large_correction_required"


def test_post_signoff_non_material_request_is_forced_material_for_dish_candidate(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    started = app.execute(
        "start", agent="codex", task_gid="t", kind="change",
        change_level="small", change_reason="rename candidate", run_id="later-editor",
    )
    assert started["ok"]
    candidate = tmp_path / "candidate-change.txt"
    candidate.write_text(f"{backend.title}\n{backend.notes}".replace(
        "Dish candidate: Test dish", "Dish candidate: Different dish"
    ))
    prepared = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="non-material",
    )
    assert prepared["ok"]
    row = app.conn.execute(
        "SELECT status,phase,inherited_signoff_cycle_id FROM operations WHERE operation_id=?",
        (started["submission_id"],),
    ).fetchone()
    assert tuple(row) == ("open", "await_verification", None)
    assert "Status: pending-verification" in backend.notes
    assert "Verified by: None" in backend.notes
    intent = app.conn.execute(
        """SELECT intended_json,completed_at FROM operation_steps
             WHERE operation_id=? AND step_name='change_intent'""",
        (started["submission_id"],),
    ).fetchone()
    assert intent["completed_at"] is not None
    assert intent["intended_json"] == '{"level":"small","reason":"rename candidate"}'
    assert (
        "Codex — gpt-5.6-sol — updated the candidate — rename candidate — "
        "Small — pending-verification"
    ) in backend.notes


def test_evidence_hold_blocks_content_drift_before_resolution(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app)
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="evidence",
        reason="confirm source", resume_status="pending-verification", run_id="review",
    )
    assert held["ok"]
    backend.notes = backend.notes.replace("100 g test ingredient", "500 g test ingredient")
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release("verification"))
    result = admin.execute(
        "supply-evidence", submission_id=operation_id,
        detail="Marco confirmed source", resume_status="pending-verification",
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "live_task_drift"
    assert app.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()[0] == 1


def test_human_hold_blocks_placement_drift_before_resolution(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review(app)
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="human-review",
        reason="Marco must decide", resume_status="pending-verification", run_id="review",
    )
    assert held["ok"]
    backend.section = "12345"
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release("verification"))
    result = admin.execute(
        "record-human-decision", submission_id=operation_id,
        detail="Marco decided", resume_status="pending-verification",
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "live_task_placement_drift"


def test_inspect_suppresses_verify_and_submit_after_exact_content_drift(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    backend.title += " externally changed"
    inspected = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["allowed_actions"] == []
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review")
    assert review["code"] == "CONFLICT"


def test_non_material_checkin_requires_exact_local_signed_baseline(tmp_path):
    # Produce a genuinely ready live task, then attach a fresh local database
    # that has exact content evidence but no local approved Verification cycle.
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_app, backend, source_operation, _ = make_app(source_dir)
    review = _review(source_app, run="source-review")
    approved = source_app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=source_operation,
        correction="none", reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="source-review",
    )
    assert approved["ok"]

    from dish_tool.commands import DishApplication
    from dish_tool.database import confirm_task_content, initialize_database
    fresh = DishApplication(
        initialize_database(tmp_path / "fresh.db"), backend,
        release_loader=lambda role=None: source_app._load_release(role),
    )
    confirm_task_content(
        fresh.conn, task_gid="t", title=backend.title, notes=backend.notes,
        schema_version="2", boundary="imported-ready-text",
    )
    started = fresh.execute(
        "start", agent="gpt", task_gid="t", kind="change",
        change_level="small", change_reason="handling wording", run_id="later",
    )
    assert started["ok"]
    candidate = tmp_path / "non-material.txt"
    candidate.write_text(f"{backend.title}\n{backend.notes}".replace("1. Cook it.", "1. Cook it gently."))
    result = fresh.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="non-material",
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "non_material_signed_baseline_missing"


def test_decisions_authorization_preserves_typed_values(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release("verification"))
    result = admin.execute(
        "authorize-governed-change", submission_id=operation_id,
        field="Decisions", before=[], after=["Human — Marco: use route B"],
        reason="Marco chose route B", run_id="marco",
    )
    assert result["ok"]
    rows = reserve_marco_authorizations(
        app.conn, task_gid="t", operation_id=operation_id,
        changes=({"field": "Decisions", "before": (), "after": ("Human — Marco: use route B",)},),
    )
    assert len(rows) == 1


def test_two_pass_reset_requires_one_operative_replacement_hunk():
    from dish_tool.step8 import _prove_reset

    before = _doc(TASK.replace("100 g test ingredient", "130 g test ingredient"))
    fake = _doc(TASK.replace(
        "100 g test ingredient",
        "0.13 kg test ingredient\nFuture target: 140 g test ingredient",
    ))
    with pytest.raises(DishRuleError) as exc:
        _prove_reset(before, fake, "scope", "130 g test ingredient", "140 g test ingredient")
    assert exc.value.rule == "two_pass_reset_not_applied"

    genuine = _doc(TASK.replace("100 g test ingredient", "140 g test ingredient"))
    assert _prove_reset(before, genuine, "scope", "130 g test ingredient", "140 g test ingredient") == "sections.QUANTITIES"
