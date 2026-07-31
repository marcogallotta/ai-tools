from pathlib import Path


from dish_tool.database import record_marco_authorization
from tests.support.verification import TASK, make_app


def _review(app, run="review"):
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id=run, independence_attestation="independent")
    assert result["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result


def test_small_cannot_self_authorize_lock_change(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = _review(app)
    candidate = tmp_path / "small.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Remove crispness constraint").replace(
        "Decisions:\nNone", "Decisions:\nHuman — Marco: Authorizes Locks change: remove crispness"
    ))
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="small",
        file_path=str(candidate), reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"][0]["rule"] in {"small_correction_scope", "governed_change_unauthorized", "large_correction_required"}


def test_constructor_run_from_prior_operation_can_verify_later_operation(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    app.conn.execute(
        "INSERT INTO operation_actor_facts(fact_id,operation_id,task_gid,role,agent,run_id,created_at) VALUES ('old',?,?,?,?,?,datetime('now'))",
        (operation_id, "t", "constructor", "gpt", "constructor-run"),
    )
    app.conn.execute("UPDATE operations SET status='completed', phase='terminal', completed_at=datetime('now') WHERE operation_id=?", (operation_id,))
    from dish_tool.database import create_operation, assert_fresh_verifier
    from dish_tool.models import OperationActors
    op2 = create_operation(
        app.conn,
        task_gid="t",
        operation_kind="change",
        expected_identity=app.conn.execute("SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'").fetchone()[0],
        schema_version="2",
        actors=OperationActors(editor_agent="codex", run_id="later-editor"),
    )
    assert_fresh_verifier(
        app.conn,
        operation_id=op2["operation_id"],
        agent="gpt",
        run_id="constructor-run",
        independence_attestation=None,
    )
