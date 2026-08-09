import shlex
import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.database import reserve_marco_authorizations
from dish_tool.errors import DishRuleError
from dish_tool.governed_diff import explicit_material_reasons
from dish_tool.task_document import parse_task_document
from tests.support.readiness import _approve_and_submit
from tests.support.verification import TASK, make_app, review_and_inspect


def _doc(text=TASK):
    return parse_task_document(text)


def test_evidence_hold_blocks_content_drift_before_resolution(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app)
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
    review_and_inspect(app)
    held = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="human-review",
        reason="Marco must decide", resume_status="pending-verification", run_id="review",
        human_review_confirmed=True,
        human_review_basis="Only Marco can resolve the remaining choice within settled authority.",
        repairs_considered="Plausible within-authority repairs were considered and do not resolve the choice.",
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
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review", independence_attestation="independent")
    assert review["code"] == "CONFLICT"


def test_non_material_checkins_preserve_signoff_lineage_across_multiple_heads(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    source_cycle = app.conn.execute(
        "SELECT cycle_id FROM verification_cycles WHERE operation_id=? AND outcome='approved'",
        (operation_id,),
    ).fetchone()["cycle_id"]

    first = app.execute(
        "start", agent="codex", task_gid="t", kind="change",
        change_level="small", change_reason="clarify gentle handling", run_id="lineage-one",
    )
    first_candidate = tmp_path / "lineage-one.txt"
    first_live = f"{backend.title}\n{backend.notes}"
    first_text = first_live.replace("1. Cook it.", "1. Cook it gently.")
    assert first_text != first_live
    assert explicit_material_reasons(_doc(first_live), _doc(first_text)) == ()
    first_candidate.write_text(first_text)
    first_result = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=first["submission_id"], file_path=str(first_candidate),
        material_classification="non-material",
    )
    assert first_result["ok"]
    assert first_result["data"]["material_classification"] == {
        "classified_subject": "canonical body diff from the signed baseline",
        "requested": "non-material",
        "effective": "non-material",
        "forced_material_reasons": [],
        "route": "signed-check-in",
    }

    second = app.execute(
        "start", agent="gpt", task_gid="t", kind="change",
        change_level="small", change_reason="clarify brief handling", run_id="lineage-two",
    )
    second_candidate = tmp_path / "lineage-two.txt"
    second_live = f"{backend.title}\n{backend.notes}"
    second_text = second_live.replace(
        "1. Cook it gently.",
        "1. Cook it gently.\n2. Finish freshly just before serving.",
    )
    assert second_text != second_live
    assert explicit_material_reasons(_doc(second_live), _doc(second_text)) == ()
    second_candidate.write_text(second_text)
    second_result = app.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=second["submission_id"], file_path=str(second_candidate),
        material_classification="non-material",
    )
    assert second_result["ok"], second_result
    assert second_result["data"]["material_classification"] == {
        "classified_subject": "canonical body diff from the signed baseline",
        "requested": "non-material",
        "effective": "non-material",
        "forced_material_reasons": [],
        "route": "signed-check-in",
    }
    rows = app.conn.execute(
        """SELECT operation_id,inherited_signoff_cycle_id
             FROM operations
            WHERE terminal_outcome='non_material_checkin'
            ORDER BY completed_at"""
    ).fetchall()
    assert [row["inherited_signoff_cycle_id"] for row in rows] == [source_cycle, source_cycle]


def test_change_start_requires_exact_local_signed_baseline(tmp_path):
    # Produce a genuinely ready live task, then attach a fresh local database
    # that has exact content evidence but no local approved Verification cycle.
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_app, backend, source_operation, _ = make_app(source_dir)
    review = review_and_inspect(source_app, run_id="source-review")
    approved = source_app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=source_operation,
        correction="none", reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="source-review",
    )
    assert approved["ok"]

    from dish_tool.commands import DishApplication
    from dish_tool.database import confirm_task_content
    from dish_tool.database_initialization import initialize_database
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
    assert not started["ok"]
    assert started["code"] == "WRONG_STATE"
    assert started["errors"][0]["rule"] == "post_signoff_change_signed_baseline_required"
    assert fresh.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_post_planning_priors_change_requires_exact_marco_authorization(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    started = app.execute(
        "start", agent="codex", task_gid="t", kind="change",
        change_level="small", change_reason="record prior route", run_id="later-editor",
    )
    assert started["ok"]
    candidate = tmp_path / "priors-change.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Priors: None", "Priors: Earlier steamed route was too soft"
        )
    )
    blocked = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="non-material",
    )
    assert blocked["code"] == "VALIDATION_FAILED"
    error = blocked["errors"][0]
    assert error["rule"] == "governed_change_unauthorized"
    assert error["required_admin_action"] == "authorize-governed-change"
    argv = shlex.split(error["admin_command"])
    assert argv[:3] == [
        "dish-admin", "authorize-governed-change", started["submission_id"]
    ]
    assert argv[argv.index("--field") + 1] == "Priors"
    assert argv[argv.index("--before") + 1] == '"None"'
    assert argv[argv.index("--after") + 1] == '"Earlier steamed route was too soft"'
    assert error["human_action"]["effect"].startswith(
        "Create one operation-bound authorization"
    )
    assert error["human_action"]["details"]
    assert error["human_action"]["context"]["governed_change"] == {
        "field": "Priors",
        "before": "None",
        "after": "Earlier steamed route was too soft",
        "added_tokens": [],
        "removed_tokens": [],
        "scope": "this task, operation, and exact proposed values",
        "modifies_task": False,
        "after_success": "retry the same unchanged candidate",
        "proposal_reason": None,
        "linked_changes": [],
    }
    assert "Before showing any command, explain" in error["directive"]
    assert "dish-admin authorize-governed-change" not in error["directive"]

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release("verification")
    )
    authorized = admin.execute(
        "authorize-governed-change", submission_id=started["submission_id"],
        field="Priors", before="None", after="Earlier steamed route was too soft",
        reason="Marco authorized recording the prior result", run_id="marco-priors",
    )
    assert authorized["ok"]
    prepared = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="non-material",
    )
    assert prepared["ok"]
    assert prepared["data"]["material_classification"]["effective"] == "non-material"


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


def test_verification_hold_reset_requires_one_operative_replacement_hunk():
    from dish_tool.step8 import _prove_reset

    before = _doc(TASK.replace("100 g test ingredient", "130 g test ingredient"))
    fake = _doc(TASK.replace(
        "100 g test ingredient",
        "0.13 kg test ingredient\nFuture target: 140 g test ingredient",
    ))
    with pytest.raises(DishRuleError) as exc:
        _prove_reset(before, fake, "scope", "130 g test ingredient", "140 g test ingredient")
    assert exc.value.rule == "verification_hold_reset_not_applied"

    genuine = _doc(TASK.replace("100 g test ingredient", "140 g test ingredient"))
    assert _prove_reset(before, genuine, "scope", "130 g test ingredient", "140 g test ingredient") == "sections.QUANTITIES"
