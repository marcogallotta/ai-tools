import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.admin import DishAdminApplication
from test_dish_tool_step7_verification import make_app


def _signed(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="submit-review", independence_attestation="independent")
    approved = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="submit-review",
        independence_attestation="independent",
    )
    assert approved["ok"]
    return app, backend, operation_id


def test_submit_is_movement_only_and_moves_vq_once(tmp_path):
    app, backend, operation_id = _signed(tmp_path)
    before = (backend.title, backend.notes, backend.writes)
    result = app.execute("submit", submission_id=operation_id)
    assert result["ok"] and result["data"]["handoff"] == "moved_to_destination"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
        "movement-confirmation",
    ]
    assert "agent-semantic-review" not in result["data"]["validation_scope"]
    assert backend.section == "12345" and backend.moves == 2
    assert (backend.title, backend.notes, backend.writes) == before
    retry = app.execute("submit", submission_id=operation_id)
    assert retry["code"] == "WRONG_STATE"
    assert backend.moves == 2


def test_already_at_destination_is_idempotent_without_content_write(tmp_path):
    app, backend, operation_id = _signed(tmp_path)
    backend.section = "12345"
    writes = backend.writes
    result = app.execute("submit", submission_id=operation_id)
    assert result["ok"] and result["data"]["handoff"] == "already_at_destination"
    assert backend.moves == 1 and backend.writes == writes


def test_research_and_manual_placement_are_preserved(tmp_path):
    for section, handoff in (("rq", "research_queue_preserved"), ("ref", "manual_placement_preserved")):
        app, backend, operation_id = _signed(tmp_path / section)
        backend.section = section
        result = app.execute("submit", submission_id=operation_id)
        assert result["ok"] and result["data"]["handoff"] == handoff
        assert backend.section == section


def test_missing_destination_keeps_ready_and_reports_diagnostic(tmp_path):
    app, backend, operation_id = _signed(tmp_path)
    # A post-signoff content edit must not be able to rebind signoff by inserting
    # a later content version under the same operation.
    backend.notes = backend.notes.replace("Destination section: Sichuan — 12345", "Destination section: [destination missing]")
    backend.title = backend.title.replace("[non-main] ", "[non-main] [destination missing] ", 1)
    from dish_tool.database import confirm_task_content
    confirm_task_content(app.conn, task_gid="t", title=backend.title, notes=backend.notes, schema_version="2", operation_id=operation_id, boundary="content_write")
    result = app.execute("submit", submission_id=operation_id)
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "post_signoff_content_drift"
    assert "Status: ready" in backend.notes and backend.section == "vq"


def test_changed_content_after_signoff_blocks_movement(tmp_path):
    app, backend, operation_id = _signed(tmp_path)
    backend.notes = backend.notes.replace("Crisp and aromatic.", "Crisp and very aromatic.")
    moves = backend.moves
    result = app.execute("submit", submission_id=operation_id)
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "post_signoff_content_drift"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
        "movement-confirmation",
    ]
    assert backend.moves == moves


def test_admin_recovery_uses_live_reread_evidence(tmp_path):
    app, backend, operation_id = _signed(tmp_path)
    admin = DishAdminApplication(app.conn, backend=backend)
    result = admin.execute("recover", submission_id=operation_id, outcome="inspect", reason="check live")
    assert result["ok"]
    assert result["data"]["live_identity"]
    assert result["data"]["live_section_gid"] == "vq"
    assert result["data"]["content_recovery_state"] == "confirmed_signoff"
