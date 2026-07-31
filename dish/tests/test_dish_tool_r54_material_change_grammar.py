from __future__ import annotations

import pytest


def test_material_change_grammar_reports_all_detectable_subfields():
    from dish_tool.task_document import parse_task_document, validate_task_document
    from tests.test_dish_tool_step2_canonical import TASK as CANONICAL_TASK

    invalid = CANONICAL_TASK.replace(
        "2026-07-25 — ChatGPT — GPT-5 — tightened hydration — improve crispness — Large — pending-verification",
        "25/07/2026 — GPT —  —  —  — Medium — verified — GPT, bad, someday",
    )
    findings = validate_task_document(parse_task_document(invalid)).findings
    rules = {finding.rule for finding in findings}
    assert {
        "material-changes.format",
        "material-changes.date",
        "material-changes.agent",
        "material-changes.model",
        "material-changes.change",
        "material-changes.reason",
        "material-changes.materiality",
        "material-changes.verification",
    } <= rules
    format_finding = next(
        finding for finding in findings if finding.rule == "material-changes.format"
    )
    assert "exactly seven fields" not in format_finding.message
    assert "<YYYY-MM-DD>" in format_finding.message
    assert "<Small|Large>" in format_finding.message


def test_material_change_approval_finalizes_pending_entry_and_survives_restart(tmp_path):
    from dish_tool.database import initialize_database
    from dish_tool.task_document import parse_task_document
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")

    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="change-editor",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    candidate = tmp_path / "material-change.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, operation_id)
    prepared = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=str(candidate),
        material_classification="material",
        run_id="change-editor",
    )
    assert prepared["ok"]
    assert "Small — pending-verification" in backend.notes

    review = _review(application, run="change-review", agent="codex")
    approved = application.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="change-review",
    )
    assert approved["ok"]
    document = parse_task_document(f"{backend.title}\n{backend.notes}")
    assert "Small — verified — Codex, self-reported model: gpt-5.6-sol," in document.material_changes[-1]
    assert not document.material_changes[-1].endswith(" — pending-verification")

    submitted = application.execute("submit", submission_id=operation_id)
    assert submitted["ok"]
    signed_identity = approved["data"]["signed_identity"]
    application.conn.close()

    reopened = initialize_database(tmp_path / "dish.db")
    try:
        cycle = reopened.execute(
            """SELECT signed_identity,signed_content_version_id
                 FROM verification_cycles
                WHERE operation_id=? AND outcome='approved'""",
            (operation_id,),
        ).fetchone()
        assert cycle["signed_identity"] == signed_identity
        version = reopened.execute(
            "SELECT notes FROM content_versions WHERE content_version_id=?",
            (cycle["signed_content_version_id"],),
        ).fetchone()
        assert "Small — verified — Codex, self-reported model: gpt-5.6-sol," in version["notes"]
        assert "Small — pending-verification" not in version["notes"]
    finally:
        reopened.close()


def test_approval_finalizes_rejected_change_and_corrective_change(tmp_path):
    from dish_tool.task_document import parse_task_document
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")

    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="large",
        change_reason="adjust the cooking method",
        run_id="change-editor",
    )
    operation_id = started["submission_id"]
    candidate = tmp_path / "first-material-edit.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "1. Cook it.", "1. Cook it quickly."
        )
    )
    assert application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=str(candidate),
        material_classification="material",
        run_id="change-editor",
    )["ok"]

    first_review = _review(application, "first-change-review")
    corrected = tmp_path / "corrective-material-edit.txt"
    corrected.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "1. Cook it quickly.", "1. Cook it gently."
        )
    )
    rejected = application.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        route="large",
        reason="the quick method would produce the wrong texture",
        file_path=str(corrected),
        run_id="first-change-review",
    )
    assert rejected["ok"]
    rejected_document = parse_task_document(f"{backend.title}\n{backend.notes}")
    assert len(rejected_document.material_changes) == 2
    assert all(
        line.endswith(" — pending-verification")
        for line in rejected_document.material_changes
    )

    final_review = _review(application, "corrective-review", agent="gpt")
    approved = application.execute(
        "approve",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=final_review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="corrective-review",
    )
    assert approved["ok"]

    signed = parse_task_document(f"{backend.title}\n{backend.notes}")
    assert len(signed.material_changes) == 2
    assert all(
        " — verified — Custom GPT, self-reported model: gpt-5.6-sol, " in line
        for line in signed.material_changes
    )
    assert not any(
        line.endswith(" — pending-verification")
        for line in signed.material_changes
    )


def test_submit_refuses_ready_task_with_any_material_change_pending(tmp_path, monkeypatch):
    import dataclasses

    from dish_tool.database import content_identity
    from dish_tool import step9
    from dish_tool.task_document import parse_task_document
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="change-editor",
    )
    operation_id = started["submission_id"]
    candidate = tmp_path / "material-change-pending.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, operation_id)
    assert application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=str(candidate),
        material_classification="material",
        run_id="change-editor",
    )["ok"]
    review = _review(application, run="change-review", agent="codex")
    approved = application.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="change-review",
    )
    assert approved["ok"]

    signed = parse_task_document(f"{backend.title}\n{backend.notes}")
    latest = signed.material_changes[-1]
    pending = latest.split(" — verified — ", 1)[0] + " — pending-verification"
    corrupted = dataclasses.replace(
        signed,
        material_changes=signed.material_changes[:-1] + (pending, latest),
    )
    lines = corrupted.render().splitlines()
    backend.title = lines[0]
    backend.notes = "\n".join(lines[1:]) + "\n"
    identity = content_identity(backend.title, backend.notes).digest
    monkeypatch.setattr(step9, "_signed_identity", lambda conn, op_id: identity)

    with pytest.raises(Exception) as exc_info:
        step9.submit_live(application.conn, backend, operation_id=operation_id)
    error = exc_info.value
    assert getattr(error, "rule", None) == "material_change_verification_pending"
    row = application.conn.execute(
        "SELECT status,phase,terminal_outcome FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert tuple(row) == ("open", "await_submission", None)


def test_post_signoff_change_cannot_rewrite_material_change_history(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate, _review
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")

    first = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="first-editor",
    )
    first_candidate = tmp_path / "first-material-change.txt"
    first_candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, first["submission_id"])
    assert application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=first["submission_id"],
        file_path=str(first_candidate),
        material_classification="material",
        run_id="first-editor",
    )["ok"]
    review = _review(application, run="first-review", agent="codex")
    approved = application.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=first["submission_id"],
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="first-review",
    )
    assert approved["ok"]
    assert application.execute("submit", submission_id=first["submission_id"])["ok"]

    second = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="handling clarification",
        run_id="second-editor",
    )
    tampered = f"{backend.title}\n{backend.notes}".replace(
        "updated the candidate", "rewrote the historical description"
    ).replace("1. Cook it.", "1. Cook it gently.")
    second_candidate = tmp_path / "tampered-history.txt"
    second_candidate.write_text(tampered)
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=second["submission_id"],
        file_path=str(second_candidate),
        material_classification="non-material",
        run_id="second-editor",
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] == "material_change_history_modified"
    assert result["errors"][0]["authority"] == (
        "Dish appends and finalizes the current workflow entry"
    )


def test_material_classification_is_required_only_for_changed_post_signoff_body(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="clarify serving handling",
        run_id="later-editor",
    )
    candidate = tmp_path / "classification-required.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace("1. Cook it.", "1. Cook it gently.")
    )
    missing = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
        run_id="later-editor",
    )
    assert missing["code"] == "INVALID_ARGUMENT"
    assert missing["errors"][0]["rule"] == "material_classification_required"
    assert missing["errors"][0]["classified_subject"] == (
        "canonical body diff from the signed baseline"
    )


def test_material_classification_reports_effective_route_and_forced_reasons(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_r42_authority_matrix import _authorize_dish_candidate
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="rename candidate",
        run_id="later-editor",
    )
    candidate = tmp_path / "classification-forced.txt"
    candidate.write_text(
        f"{backend.title}\n{backend.notes}".replace(
            "Dish candidate: Test dish", "Dish candidate: Different dish"
        )
    )
    _authorize_dish_candidate(application, backend, started["submission_id"])
    prepared = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
        material_classification="non-material",
        run_id="later-editor",
    )
    classification = prepared["data"]["material_classification"]
    assert classification == {
        "classified_subject": "canonical body diff from the signed baseline",
        "requested": "non-material",
        "effective": "material",
        "forced_material_reasons": ["dish_candidate"],
        "route": "verification",
    }


def test_material_classification_is_rejected_when_no_body_diff_exists(tmp_path):
    from tests.test_dish_tool_r27_r29_readiness import _approve_and_submit
    from tests.test_dish_tool_step7_verification import make_app

    application, backend, initial_operation, _ = make_app(tmp_path)
    _approve_and_submit(application, initial_operation, run="initial-review")
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="change",
        change_level="small",
        change_reason="no-op probe",
        run_id="later-editor",
    )
    candidate = tmp_path / "classification-no-diff.txt"
    candidate.write_text(f"{backend.title}\n{backend.notes}")
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=str(candidate),
        material_classification="non-material",
        run_id="later-editor",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "material_classification_not_applicable"

