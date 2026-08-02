import pytest
from pathlib import Path


from dish_tool.admin import DishAdminApplication
from tests.support.verification import TASK, make_app, review_and_inspect


@pytest.mark.smoke
def test_small_correction_is_written_rechecked_and_signed_same_pass(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = review_and_inspect(app, agent="codex")
    candidate = tmp_path / "small.txt"
    candidate.write_text(TASK.replace("1. Cook it.", "1. Cook it gently."))
    result = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="small", file_path=str(candidate),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
    )
    assert result["ok"]
    assert "applied a small Verification correction" in backend.notes
    assert "Small — verified — Codex, self-reported model: gpt-5.6-sol," in backend.notes
    assert "Status: ready" in backend.notes
    cycle = app.conn.execute("SELECT correction_class, outcome FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()
    assert tuple(cycle) == ("small", "approved")


@pytest.mark.smoke
def test_large_requires_fresh_verifier_and_third_failure_writes_task_hold(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"
    for index, (agent, run_id, amount) in enumerate((("codex", "first", "120 g"), ("gpt", "second", "130 g"), ("claude", "third", "140 g")), start=1):
        review_and_inspect(app, agent=agent, run_id=run_id)
        candidate.write_text(TASK.replace("100 g", amount))
        result = app.execute("reject", agent=agent, model="gpt-5.6-sol", submission_id=operation_id, route="large", reason=f"failure {index}", file_path=str(candidate), run_id=run_id)
        assert result["ok"]
        if index < 3:
            assert result["data"]["verification_hold"] is False
            assert result["data"]["new_cycle_id"]
            assert result["allowed_actions"] == ["start"]
        else:
            assert result["data"]["verification_hold"] is True
    assert "Status: pending-human-review" in backend.notes
    assert "Resume status: pending-verification" in backend.notes
    assert "140 g" in backend.notes
    blocked = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="fourth", independence_attestation="independent")
    assert blocked["code"] == "WRONG_STATE"


@pytest.mark.smoke
def test_evidence_and_human_routes_require_protocol_reasons_and_resume(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex")
    bad = app.execute("reject", agent="codex", submission_id=operation_id, route="evidence", reason="missing source", resume_status=None, run_id="review")
    assert bad["code"] == "INVALID_ARGUMENT"
    good = app.execute("reject", agent="codex", submission_id=operation_id, route="evidence", reason="Marco must confirm the factual input", resume_status="pending-verification", run_id="review")
    assert good["ok"] and "Status: pending-evidence" in backend.notes
    assert good["allowed_actions"] == []
    assert good["data"]["required_admin_action"] == "supply-evidence"
    assert good["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
        "agent-semantic-review",
    ]
    assert "provenance-signoff" not in good["data"]["validation_scope"]
    assert good["data"]["continuation_surface"] == "private-admin"
    assert good["data"]["connected_action_available"] is False
    assert good["data"]["admin_command"] == (
        f'dish-admin supply-evidence {operation_id} --detail "<summarize the supplied evidence>" '
        "--resume-status pending-verification"
    )
    assert good["data"]["after_resolution"] == {
        "legal_actions": ["start"], "required_start_kind": "verification", "phase": "await_verification",
    }
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["data"]["admin_command"] == good["data"]["admin_command"]
    assert inspected["data"]["continuation_surface"] == "private-admin"




@pytest.mark.smoke
def test_human_review_route_reports_private_continuation_without_exposing_it(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="human-review")
    result = app.execute(
        "reject", agent="codex", submission_id=operation_id,
        route="human-review", reason="Marco must choose between two valid serving formats.",
        resume_status="pending-verification", run_id="human-review",
    )
    assert result["ok"]
    assert result["allowed_actions"] == []
    assert result["data"]["required_admin_action"] == "record-human-decision"
    assert "Status: pending-human-review" in backend.notes

@pytest.mark.smoke
def test_marco_reopen_requires_substantive_change_and_retains_cycles(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    candidate = tmp_path / "large.txt"; candidate.write_text(TASK.replace("100 g", "120 g"))
    review_and_inspect(app, agent="codex", run_id="one")
    app.execute("reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, route="large", reason="first", file_path=str(candidate), run_id="one")
    review_and_inspect(app, agent="gpt", run_id="two")
    candidate.write_text(TASK.replace("100 g", "130 g"))
    app.execute("reject", agent="gpt", model="gpt-5.6-sol", submission_id=operation_id, route="large", reason="second", file_path=str(candidate), run_id="two")
    review_and_inspect(app, agent="claude", run_id="three")
    candidate.write_text(TASK.replace("100 g", "140 g"))
    app.execute("reject", agent="claude", model="claude-sonnet", submission_id=operation_id, route="large", reason="third", file_path=str(candidate), run_id="three")
    admin = DishAdminApplication(app.conn, backend=backend)
    bad = admin.execute("reopen", submission_id=operation_id, category="hash", before="a", after="b", editor="codex", model="gpt-5.6-sol", run_id="reopen-run", file_path=str(candidate), date="2026-07-25")
    assert bad["code"] == "INVALID_ARGUMENT"
    corrected = tmp_path / "reopened.txt"
    corrected.write_text(f"{backend.title}\n{backend.notes}".replace("Compare hydration routes.", "Compare hydration routes with a rested-starch reset."))
    result = admin.execute(
        "reopen", submission_id=operation_id, category="premise",
        before="Compare hydration routes.",
        after="Compare hydration routes with a rested-starch reset.",
        editor="codex", model="gpt-5.6-sol", run_id="reopen-run", file_path=str(corrected), date="2026-07-25",
    )
    assert result["ok"]
    assert "Status: pending-verification" in backend.notes
    assert (
        "reset premise at sections.WHY COOK IT from Compare hydration routes. "
        "to Compare hydration routes with a rested-starch reset."
    ) in backend.notes
    assert "Large — pending-verification" in backend.notes
    assert app.conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()[0] == 4


@pytest.mark.smoke
def test_small_correction_cannot_replace_unreviewed_live_content(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = review_and_inspect(app, agent="codex", run_id="small-proof")
    backend.title = backend.title.replace("Test dish", "Externally changed dish")
    candidate = tmp_path / "small-unreviewed.txt"
    candidate.write_text(TASK.replace("1. Cook it.", "1. Cook it gently."))
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="small",
        file_path=str(candidate), reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="small-proof",
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "live_task_drift"


@pytest.mark.smoke
def test_reject_requires_exact_verifier_run_proof(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review_and_inspect(app, agent="codex", run_id="reject-proof")
    result = app.execute(
        "reject", agent="codex", submission_id=operation_id, route="evidence",
        reason="missing evidence", resume_status="pending-verification", run_id="wrong-run",
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "verifier_proof_mismatch"


@pytest.mark.smoke
def test_approve_rejects_candidate_file_without_small_correction(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-extra-file", independence_attestation="independent")
    candidate = tmp_path / "unused.txt"
    candidate.write_text(TASK)
    result = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="none", file_path=str(candidate),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review-extra-file",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "approval_file_unexpected"


@pytest.mark.smoke
def test_small_correction_requires_candidate_file(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-missing-file", independence_attestation="independent")
    result = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="small", reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review-missing-file",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "small_correction_file_required"


@pytest.mark.smoke
def test_hold_routes_reject_large_only_arguments(tmp_path):
    for suffix, extra, rule in (
        ("file", {"file_path": str(tmp_path / "unused.txt")}, "hold_candidate_unexpected"),
        ("model", {"model": "gpt-5.6-sol"}, "hold_model_unexpected"),
    ):
        path = tmp_path / "unused.txt"
        path.write_text(TASK)
        case_dir = tmp_path / suffix
        case_dir.mkdir()
        app, _backend, operation_id, _ = make_app(case_dir)
        review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id=f"review-{suffix}", independence_attestation="independent")
        assert review["ok"]
        assert app.execute("inspect", agent="codex", submission_id=operation_id)["ok"]
        result = app.execute(
            "reject", agent="codex", submission_id=operation_id, route="evidence",
            reason="Marco must confirm a fact", resume_status="pending-verification",
            run_id=f"review-{suffix}", **extra,
        )
        assert result["code"] == "INVALID_ARGUMENT"
        assert result["errors"][0]["rule"] == rule


@pytest.mark.smoke
def test_large_route_rejects_hold_resume_status(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-large-resume", independence_attestation="independent")
    assert app.execute("inspect", agent="codex", submission_id=operation_id)["ok"]
    candidate = tmp_path / "large.txt"
    candidate.write_text(TASK.replace("100 g", "120 g"))
    result = app.execute(
        "reject", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        route="large", reason="material correction", file_path=str(candidate),
        resume_status="pending-verification", run_id="review-large-resume",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "large_resume_status_unexpected"


@pytest.mark.smoke
def test_hold_route_reports_all_incompatible_arguments_and_permitted_set(tmp_path):
    candidate = tmp_path / "unused.txt"
    candidate.write_text(TASK)
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="aggregate-route-errors",
        independence_attestation="independent",
    )
    assert review["ok"]
    assert app.execute("inspect", agent="codex", submission_id=operation_id)["ok"]
    result = app.execute(
        "reject",
        agent="codex",
        submission_id=operation_id,
        route="evidence",
        reason="Marco must confirm a fact",
        file_path=str(candidate),
        model="gpt-5.6-sol",
        independence_attestation="not accepted on this route",
        run_id="aggregate-route-errors",
    )
    assert result["code"] == "INVALID_ARGUMENT"
    rules = {item["rule"] for item in result["errors"]}
    assert {
        "hold_candidate_unexpected",
        "hold_model_unexpected",
        "hold_independence_attestation_unexpected",
        "resume_status_required",
        "rejection_route_arguments_invalid",
    }.issubset(rules)
    overall = next(
        item for item in result["errors"] if item["rule"] == "rejection_route_arguments_invalid"
    )
    assert overall["permitted_arguments"] == [
        "submission_id", "agent", "reason", "route", "resume_status"
    ]
