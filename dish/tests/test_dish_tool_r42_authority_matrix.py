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
    result = app.execute("start", agent=agent, task_gid="t", kind="verification", run_id=run, independence_attestation="independent")
    assert result["ok"]
    return result


def _authorize_dish_candidate(app, backend, operation_id, *, before="Test dish", after="Different dish"):
    admin = DishAdminApplication(
        app.conn, backend=backend,
        release_loader=lambda: app._load_release("verification"),
    )
    result = admin.execute(
        "authorize-governed-change", submission_id=operation_id,
        field="Dish candidate", before=before, after=after,
        reason="Marco authorized the candidate identity change", run_id="marco",
    )
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


def _with_section(document, section, text):
    return dataclasses.replace(document, sections={**document.sections, section: text})


@pytest.mark.parametrize(
    "section",
    ["HOW TO COOK IT", "CHECK BEFORE COOKING", "WATCH OUT FOR", "STORAGE"],
)
def test_equivalent_freshness_handling_is_small_in_any_handling_section(section):
    before = _doc()
    old = before.sections.get(section, "")
    after = _with_section(
        before,
        section,
        f"{old}\nReheat the chicken first and fold in fresh basil after reheating to preserve aroma.".strip(),
    )
    assert explicit_material_reasons(before, after) == ()
    require_small_scope(before, after)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "Keep the lime mixed into the dish.",
            "Keep the lime separate and divide it per sitting instead of mixing it into the stored batch.",
        ),
        (
            "Reheat gently and add fresh coriander.",
            "Reheat the chicken first, then add fresh coriander after reheating.",
        ),
    ],
)
def test_storage_freshness_and_reheating_corrections_are_small(old, new):
    before = _with_section(_doc(), "STORAGE", old)
    after = _with_section(before, "STORAGE", new)
    assert explicit_material_reasons(before, after) == ()
    require_small_scope(before, after)


@pytest.mark.parametrize(
    ("old", "new", "expected_reason"),
    [
        ("Refrigerate and use within 2 days.", "Refrigerate and use within 7 days.", "section:storage"),
        ("Keep chilled.", "Keep at room temperature.", "section:storage"),
        (
            "Keep the cooked chicken separate.",
            "Stir 200 g cooked chicken into the stored batch before chilling.",
            "section:storage",
        ),
        (
            "Keep the sauce separate from the chicken.",
            "Combine the sauce with the chicken before storing the batch.",
            "section:storage",
        ),
    ],
)
def test_material_storage_changes_still_require_large(old, new, expected_reason):
    before = _with_section(_doc(), "STORAGE", old)
    after = _with_section(before, "STORAGE", new)
    reasons = explicit_material_reasons(before, after)
    assert expected_reason in reasons
    with pytest.raises(DishRuleError) as exc:
        require_small_scope(before, after)
    assert exc.value.rule == "large_correction_required"


def test_real_small_route_accepts_fresh_basil_after_reheating(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = _review(app)
    candidate = tmp_path / "small-storage.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "\n---\n",
            (
                "\n## STORAGE\n"
                "Reheat the chicken first and fold in fresh basil after reheating "
                "to preserve aroma.\n---\n"
            ),
        )
    )
    result = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="small", file_path=str(candidate),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
        independence_attestation="independent",
    )
    assert result["ok"]
    assert result["allowed_actions"] == ["submit"]
    assert "fresh basil after reheating" in backend.notes


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
        independence_attestation="independent",
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
    unauthorized = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="non-material",
    )
    assert unauthorized["code"] == "VALIDATION_FAILED"
    assert unauthorized["retryable"] is True
    assert unauthorized["allowed_actions"] == ["prepare"]
    assert unauthorized["errors"][0] == {
        "rule": "governed_change_unauthorized",
        "field": "Dish candidate",
    }

    _authorize_dish_candidate(app, backend, started["submission_id"])
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
        "Codex — self-reported model: gpt-5.6-sol — updated the candidate — rename candidate — "
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
    first_candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace("1. Cook it.", "1. Cook it gently.")
    )
    first_result = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=first["submission_id"], file_path=str(first_candidate),
        material_classification="non-material",
    )
    assert first_result["ok"]

    second = app.execute(
        "start", agent="gpt", task_gid="t", kind="change",
        change_level="small", change_reason="clarify brief handling", run_id="lineage-two",
    )
    second_candidate = tmp_path / "lineage-two.txt"
    second_candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "1. Cook it gently.", "1. Cook it gently.\n2. Finish freshly just before serving."
        )
    )
    second_result = app.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=second["submission_id"], file_path=str(second_candidate),
        material_classification="non-material",
    )
    assert second_result["ok"], second_result
    rows = app.conn.execute(
        """SELECT operation_id,inherited_signoff_cycle_id
             FROM operations
            WHERE terminal_outcome='non_material_checkin'
            ORDER BY completed_at"""
    ).fetchall()
    assert [row["inherited_signoff_cycle_id"] for row in rows] == [source_cycle, source_cycle]


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
        independence_attestation="independent",
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
    assert blocked["errors"][0]["rule"] == "governed_change_unauthorized"

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
