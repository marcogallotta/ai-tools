import pytest
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import initialize_database
from dish_tool.models import ResolvedRelease
from tests.support.verification import (
    Backend,
    TASK,
    make_app,

)







@pytest.mark.smoke
def test_constructor_cannot_verify(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    result = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="constructor-run", independence_attestation="independent")
    assert result["code"] == "AGENT_MISMATCH"
    assert backend.writes == 1


@pytest.mark.smoke
def test_verifier_without_run_id_is_rejected_even_with_attestation(tmp_path):
    app, backend, operation_id, protocol = make_app(tmp_path)
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", independence_attestation="fresh independent ChatGPT run")
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "verifier_identity_required"


@pytest.mark.smoke
def test_approve_and_reject_require_a_current_dish_inspect_fact(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="inspect-gate",
        independence_attestation="independent"
    )
    assert review["ok"]
    assert review["allowed_actions"] == ["inspect"]

    blocked = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="none", reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="inspect-gate",
    )
    assert blocked["code"] == "WRONG_STATE"
    assert blocked["errors"][0]["rule"] == "dish_inspect_required"

    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    assert inspected["data"]["dish_inspect_fact"]["reviewed_identity"] == review["data"]["reviewed_identity"]

    approved = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="none", reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="inspect-gate",
    )
    assert approved["ok"]


@pytest.mark.smoke
def test_stale_candidate_blocks_approval(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-2", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    backend.title += " changed"
    result = app.execute("approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-2")
    assert result["code"] == "CONFLICT"
    assert backend.writes == 1


@pytest.mark.smoke
def test_approval_signs_exact_reread_without_moving_and_requires_inputs(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-3", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    missing = app.execute("approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=False, provenance_complete=True, run_id="run-3")
    assert missing["code"] == "VALIDATION_FAILED"
    assert missing["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
        "agent-semantic-review", "provenance-signoff",
    ]
    result = app.execute("approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-3")
    assert result["ok"]
    assert "Status: ready" in backend.notes
    assert "Verified by: Codex — self-reported model: gpt-5.6-sol," in backend.notes
    assert backend.section == "vq" and backend.moves == 1
    assert result["allowed_actions"] == ["submit"]
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
        "agent-semantic-review", "provenance-signoff",
    ]
    row = app.conn.execute("SELECT signoff_completed_at, movement_completed_at FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    assert row["signoff_completed_at"] is not None
    assert row["movement_completed_at"] is None  # verification handoff is not final submission movement


@pytest.mark.smoke
def test_reviewed_identity_mismatch_is_retryable_with_corrected_identity(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="retry-identity",
        independence_attestation="independent",
    )
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    wrong = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, correction="none",
        reviewed_identity="0" * 64, semantic_review_complete=True,
        provenance_complete=True, run_id="retry-identity",
    )
    assert wrong["code"] == "CONFLICT"
    assert wrong["errors"][0]["rule"] == "reviewed_identity_mismatch"
    assert wrong["retryable"] is True
    corrected = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True,
        run_id="retry-identity",
    )
    assert corrected["ok"]


@pytest.mark.smoke
def test_approval_requires_model(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-model", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    result = app.execute("approve", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-model")
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_required"


@pytest.mark.smoke
def test_approval_rejects_model_with_em_dash(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-model-dash", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    result = app.execute("approve", agent="codex", model="gpt — 5.6", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-model-dash")
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_invalid_characters"


@pytest.mark.smoke
def test_approval_rejects_model_with_comma(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-model-comma", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    result = app.execute("approve", agent="codex", model="gpt-5.6, sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-model-comma")
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_invalid_characters"


@pytest.mark.smoke
def test_caller_cannot_forge_current_identity_after_review(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-forge", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    backend.title = backend.title.replace("Test dish", "Changed dish")
    from dish_tool.database import content_identity
    forged = content_identity(backend.title, backend.notes).digest
    result = app.execute("approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=forged, semantic_review_complete=True, provenance_complete=True, run_id="run-forge")
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "live_task_drift"


@pytest.mark.smoke
def test_review_and_signoff_bind_immutable_content_versions(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-bind", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    cycle = app.conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()
    assert cycle["reviewed_identity"] == review["data"]["reviewed_identity"]
    assert cycle["reviewed_content_version_id"]
    approved = app.execute("approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-bind")
    assert approved["ok"]
    cycle = app.conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()
    assert cycle["signed_identity"] == approved["data"]["signed_identity"]
    assert cycle["signed_content_version_id"]


@pytest.mark.smoke
def test_same_agent_different_run_can_verify(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    result = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="fresh-verifier-run", independence_attestation="independent")
    assert result["ok"]


@pytest.mark.smoke
def test_persisted_hash_protocol_text_survives_file_change(tmp_path):
    app, backend, operation_id, protocol = make_app(tmp_path)
    honest = tmp_path / "honest"
    (honest / "dish-verification-protocol.md").write_text("# changed later\n")
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="protocol-run", independence_attestation="independent")
    assert result["ok"]
    assert result["data"]["verification_protocol"]["text"] == protocol


@pytest.mark.smoke
def test_approval_requires_exact_verifier_run_proof(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="proof-run", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="other-run",
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "verifier_proof_mismatch"


@pytest.mark.smoke
def test_approval_requires_the_persisted_verifier_run(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="fresh-run", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True,
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "verifier_proof_mismatch"


@pytest.mark.smoke
def test_verification_start_surfaces_candidate_lineage_and_current_run_eligibility(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    result = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="fresh-verifier",
        independence_attestation="independent",
    )
    assert result["ok"]
    lineage = result["data"]["verification_lineage"]
    assert lineage["current_run"] == {
        "run_id": "fresh-verifier",
        "eligible": True,
        "rule": None,
        "prior_role": None,
    }
    assert any(
        fact["role"] == "constructor" and fact["run_id"] == "constructor-run"
        for fact in lineage["candidate_runs"]
    )


@pytest.mark.smoke
def test_inspect_surfaces_ineligible_constructor_run_before_verification_decision(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    app.invocation_run_id = "constructor-run"
    result = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert result["ok"]
    current = result["data"]["verification_lineage"]["current_run"]
    assert current["run_id"] == "constructor-run"
    assert current["eligible"] is False
    assert current["rule"] == "verifier_not_independent"
    assert current["prior_role"] == "constructor"
