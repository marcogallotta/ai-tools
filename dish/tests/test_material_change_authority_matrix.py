import dataclasses

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.database import reserve_marco_authorizations
from dish_tool.errors import DishRuleError
from dish_tool.governed_diff import explicit_material_reasons, require_small_scope
from dish_tool.task_document import parse_task_document
from tests.support.readiness import _approve_and_submit
from tests.support.verification import TASK, make_app
from tests.support.authority import (
    _authorize_dish_candidate,
    _review,

)


def _doc(text=TASK):
    return parse_task_document(text)





def test_editorial_recognition_punctuation_remains_non_material():
    before = _doc()
    after = _doc(
        TASK.replace(
            before.recognition,
            before.recognition.replace("compact side", "compact, side"),
        )
    )
    assert explicit_material_reasons(before, after) == ()
    require_small_scope(before, after)


def test_terminal_title_punctuation_and_outer_space_preserve_identity():
    before = _doc()
    lines = TASK.splitlines()
    lines[0] = f"  {lines[0]}.  "
    after = _doc("\n".join(lines))
    assert explicit_material_reasons(before, after) == ()
    require_small_scope(before, after)


def test_internal_title_punctuation_remains_non_material():
    before = _doc()
    lines = TASK.splitlines()
    lines[0] = lines[0].replace("Test dish", "Test, dish")
    after = _doc("\n".join(lines))
    assert explicit_material_reasons(before, after) == ()
    require_small_scope(before, after)


def test_substantive_recognition_change_remains_material():
    before = _doc()
    after = _doc(
        TASK.replace(
            before.recognition,
            "A durable three-sitting meal for testing texture.",
        )
    )
    assert "title_or_identity" in explicit_material_reasons(before, after)




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
    error = unauthorized["errors"][0]
    assert error["rule"] == "governed_change_unauthorized"
    assert error["field"] == "Dish candidate"
    assert error["before"] == "Test dish"
    assert error["after"] == "Different dish"
    assert error["required_admin_action"] == "authorize-governed-change"

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


def test_post_signoff_internal_comma_remains_non_material(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    started = app.execute(
        "start", agent="gpt", task_gid="t", kind="change",
        change_level="small", change_reason="editorial comma", run_id="comma-editor",
    )
    candidate = tmp_path / "comma-change.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "A compact side dish for testing texture.",
            "A compact, side dish for testing texture.",
        )
    )

    prepared = app.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="non-material",
    )

    assert prepared["ok"]
    assert prepared["state"] == "completed"
    assert prepared["data"]["handoff"] == "checked-in"
    assert prepared["data"]["verification_cycle"] is None
    assert prepared["data"]["material_classification"] == {
        "classified_subject": "canonical body diff from the signed baseline",
        "requested": "non-material",
        "effective": "non-material",
        "forced_material_reasons": [],
        "route": "signed-check-in",
    }
    assert "Status: ready" in backend.notes
    assert "Verified by: Codex" in backend.notes




def test_governed_authorization_reports_complete_linked_change_set(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    started = app.execute(
        "start", agent="codex", task_gid="t", kind="change",
        change_level="large", change_reason="align settled identity", run_id="linked-editor",
    )
    candidate = tmp_path / "linked-candidate.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}"
        .replace("Dish candidate: Test dish", "Dish candidate: Different dish")
        .replace("Purpose: Compare texture", "Purpose: Compare the settled route")
    )
    result = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="material",
    )
    assert result["code"] == "VALIDATION_FAILED"
    error = result["errors"][0]
    assert error["rule"] == "governed_change_unauthorized"
    assert [item["field"] for item in error["missing_authorizations"]] == [
        "Dish candidate", "Purpose",
    ]
    assert len(error["human_actions"]) == 2
    assert error["directive"].startswith("Before showing any command")
    assert {item["path"] for item in error["linked_candidate_changes"]} >= {
        "planning.Dish candidate", "planning.Purpose",
    }


def test_semantic_proposal_gate_rejects_non_main_with_nutrition_exemption(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _approve_and_submit(app, operation_id)
    started = app.execute(
        "start", agent="codex", task_gid="t", kind="change",
        change_level="large", change_reason="reclassify tasting", run_id="semantic-editor",
    )
    candidate = tmp_path / "incoherent-candidate.txt"
    text = f"{backend.title}\n{backend.notes}"
    lines = text.splitlines()
    lines[0] = "[non-main] " + lines[0]
    text = "\n".join(lines)
    text = text.replace("Role: main", "Role: non-main — controlled tasting portion")
    text = text.replace("Exemptions: None", "Exemptions: [nutrition-kcal] — tasting portion")
    candidate.write_text(text)
    result = app.execute(
        "prepare", agent="codex", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        material_classification="material",
    )
    assert result["code"] == "VALIDATION_FAILED"
    error = result["errors"][0]
    assert error["rule"] == "semantic_proposal_incoherent"
    assert "non-main role cannot rely" in error["contradictions"][0]
    assert "resolve every linked contradiction" in error["required_action"]
