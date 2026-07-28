import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import initialize_database
from dish_tool.models import ResolvedRelease

TASK = """[non-main] Test dish — crisp comparison side
A compact side dish for testing texture.
WHY COOK IT
Compare hydration routes.
## WHAT TO BUY
None - pantry snapshot lists required items in stock
## QUANTITIES
Portions: one sitting
100 g test ingredient
## HOW TO COOK IT
1. Cook it.
## WHAT SUCCESS LOOKS LIKE
Crisp and aromatic.
---
## PROCESS RECORD
Status: pending-research
Status detail: Continue research
Resume status: None
Verification protocol release: None
Researched by: ChatGPT — GPT-5, 2026-07-25
Verified by: None
Self-verified: ChatGPT — GPT-5, 2026-07-25
### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
### Research basis
Classification: Source-backed dish
source.example/test — Construction — hydration ratio — selected route is drier
Schema version: 2
"""


class Backend:
    def __init__(self):
        lines = TASK.splitlines()
        self.title = lines[0]
        self.notes = "\n".join(lines[1:]) + "\n"
        self.section = "rq"
        self.writes = 0
        self.moves = 0
        self.sections = [
            {"gid": "rq", "name": "Research Queue"},
            {"gid": "vq", "name": "Verification Queue"},
            {"gid": "12345", "name": "Sichuan"},
            {"gid": "ref", "name": "Reference"},
            {"gid": "src", "name": "Sourcing"},
        ]

    def list_sections(self, project_gid): return self.sections
    def read_task(self, gid):
        return {"gid": gid, "name": self.title, "notes": self.notes, "completed": False,
                "modified_at": "now", "projects": [{"gid": COOKING_PROJECT_GID}],
                "memberships": [{"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": self.section}}]}
    def update_task_content(self, *, task_gid, title, notes):
        self.writes += 1; self.title, self.notes = title, notes
    def move_task_to_section(self, *, task_gid, section_gid):
        self.moves += 1; self.section = section_gid


def make_app(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"; honest.mkdir()
    verification_text = "# Exact frozen Verification protocol\n"
    (honest / "dish-verification-protocol.md").write_text(verification_text)
    def release(role=None):
        return ResolvedRelease(version="1.0.10", commit="", root=honest,
            protocols={} if role is None else {role: verification_text if role == "verification" else f"{role} protocol"},
            manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}",
            migration_metadata={}, requested_protocol_role=role)
    app = DishApplication(initialize_database(tmp_path / "dish.db"), backend, release_loader=release)
    candidate = tmp_path / "candidate.txt"; candidate.write_text(TASK)
    start = app.execute("start", agent="gpt", task_gid="t", kind="initial", change_level=None, change_reason=None, run_id="constructor-run")
    prepared = app.execute("prepare", agent="gpt", model="gpt-5.6-sol", submission_id=start["submission_id"], file_path=str(candidate))
    assert prepared["ok"]
    return app, backend, start["submission_id"], verification_text


def test_constructor_cannot_verify(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    result = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="constructor-run", independence_attestation="independent")
    assert result["code"] == "AGENT_MISMATCH"
    assert backend.writes == 1


def test_verifier_without_run_id_is_rejected_even_with_attestation(tmp_path):
    app, backend, operation_id, protocol = make_app(tmp_path)
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", independence_attestation="fresh independent ChatGPT run")
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "verifier_identity_required"


def test_stale_candidate_blocks_approval(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-2", independence_attestation="independent")
    backend.title += " changed"
    result = app.execute("approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-2", independence_attestation="independent")
    assert result["code"] == "CONFLICT"
    assert backend.writes == 1


def test_approval_signs_exact_reread_without_moving_and_requires_inputs(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-3", independence_attestation="independent")
    missing = app.execute("approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=False, provenance_complete=True, run_id="run-3", independence_attestation="independent")
    assert missing["code"] == "VALIDATION_FAILED"
    assert missing["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
        "agent-semantic-review", "provenance-signoff",
    ]
    result = app.execute("approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-3", independence_attestation="independent")
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


def test_reviewed_identity_mismatch_is_retryable_with_corrected_identity(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = app.execute(
        "start", agent="codex", task_gid="t", kind="verification", run_id="retry-identity",
        independence_attestation="independent",
    )
    wrong = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol",
        submission_id=operation_id, correction="none",
        reviewed_identity="0" * 64, semantic_review_complete=True,
        provenance_complete=True, run_id="retry-identity",
        independence_attestation="independent",
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
        independence_attestation="independent",
    )
    assert corrected["ok"]


def test_approval_requires_model(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-model", independence_attestation="independent")
    result = app.execute("approve", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-model", independence_attestation="independent")
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_required"


def test_approval_rejects_model_with_em_dash(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-model-dash", independence_attestation="independent")
    result = app.execute("approve", agent="codex", model="gpt — 5.6", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-model-dash", independence_attestation="independent")
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_invalid_characters"


def test_approval_rejects_model_with_comma(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-model-comma", independence_attestation="independent")
    result = app.execute("approve", agent="codex", model="gpt-5.6, sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-model-comma", independence_attestation="independent")
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_invalid_characters"


def test_caller_cannot_forge_current_identity_after_review(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-forge", independence_attestation="independent")
    backend.title = backend.title.replace("Test dish", "Changed dish")
    from dish_tool.database import content_identity
    forged = content_identity(backend.title, backend.notes).digest
    result = app.execute("approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=forged, semantic_review_complete=True, provenance_complete=True, run_id="run-forge", independence_attestation="independent")
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "live_task_drift"


def test_review_and_signoff_bind_immutable_content_versions(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="run-bind", independence_attestation="independent")
    cycle = app.conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()
    assert cycle["reviewed_identity"] == review["data"]["reviewed_identity"]
    assert cycle["reviewed_content_version_id"]
    approved = app.execute("approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True, provenance_complete=True, run_id="run-bind", independence_attestation="independent")
    assert approved["ok"]
    cycle = app.conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ?", (operation_id,)).fetchone()
    assert cycle["signed_identity"] == approved["data"]["signed_identity"]
    assert cycle["signed_content_version_id"]


def test_same_agent_different_run_can_verify(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    result = app.execute("start", agent="gpt", task_gid="t", kind="verification", run_id="fresh-verifier-run", independence_attestation="independent")
    assert result["ok"]


def test_persisted_hash_protocol_text_survives_file_change(tmp_path):
    app, backend, operation_id, protocol = make_app(tmp_path)
    honest = tmp_path / "honest"
    (honest / "dish-verification-protocol.md").write_text("# changed later\n")
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="protocol-run", independence_attestation="independent")
    assert result["ok"]
    assert result["data"]["verification_protocol"]["text"] == protocol


def test_approval_requires_exact_verifier_run_proof(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="proof-run", independence_attestation="independent")
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="other-run",
        independence_attestation="independent",
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "verifier_proof_mismatch"


def test_attestation_cannot_replace_recorded_run_on_approval(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="fresh-run", independence_attestation="independent")
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True,
        independence_attestation="fresh independent run",
    )
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "verifier_proof_mismatch"


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
