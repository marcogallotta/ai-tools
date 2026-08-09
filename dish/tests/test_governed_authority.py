from pathlib import Path


from dish_tool.database import record_marco_authorization
from tests.support.verification import TASK, make_app, review_and_inspect


def test_small_lock_change_requires_large_before_embedded_decision_can_authorize(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review = review_and_inspect(app)
    candidate = tmp_path / "small.txt"
    candidate.write_text(TASK.replace("Locks: Keep crisp", "Locks: Remove crispness constraint").replace(
        "Decisions:\nNone", "Decisions:\nHuman — Marco: Authorizes Locks change: remove crispness"
    ))
    writes_before = backend.writes
    result = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="small",
        file_path=str(candidate), reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["retryable"] is True
    assert result["errors"] == [
        {
            "rule": "large_correction_required",
            "fields": [],
            "material_reasons": ["locks"],
        }
    ]
    assert backend.writes == writes_before


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
        operation_kind="initial",
        expected_identity=app.conn.execute("SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'").fetchone()[0],
        schema_version="2",
        actors=OperationActors(editor_agent="codex", run_id="later-editor"),
    )
    assert (
        assert_fresh_verifier(
            app.conn,
            operation_id=op2["operation_id"],
            agent="gpt",
            run_id="constructor-run",
            independence_attestation=None,
        )
        is None
    )


def test_attributed_decision_prefix_does_not_bypass_authorization_without_attestation(tmp_path):
    from dish_tool.errors import DishRuleError
    from dish_tool.governed_diff import require_governed_authorization
    from dish_tool.task_document import parse_task_document
    import pytest

    app, _backend, operation_id, _ = make_app(tmp_path)
    before = parse_task_document(TASK)
    after = parse_task_document(
        TASK.replace(
            "### Research basis",
            "### Decisions\nHuman — Marco: Use chicken.\n### Research basis",
        )
    )

    with pytest.raises(DishRuleError) as exc:
        require_governed_authorization(
            app.conn,
            before,
            after,
            task_gid="t",
            operation_id=operation_id,
        )
    assert exc.value.rule == "governed_change_unauthorized"

    assert require_governed_authorization(
        app.conn,
        before,
        after,
        task_gid="t",
        operation_id=operation_id,
        agent_attested_decisions=("Human — Marco: Use chicken.",),
    ) == ()
