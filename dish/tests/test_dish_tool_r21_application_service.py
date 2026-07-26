from __future__ import annotations

from dish_tool.application_service import CurrentWorkflowService
from dish_tool.errors import DishRuleError
from test_dish_tool_step7_verification import make_app


def test_current_workflow_service_is_same_authority_used_by_inspect(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    service = CurrentWorkflowService(app.conn, backend)
    before = service.authoritative_view(operation_id, schema=app._load_release(None).schema)
    inspected = app.execute("inspect", agent="gpt", submission_id=operation_id)
    assert inspected["ok"]
    assert inspected["data"]["authoritative_view"] == before
    assert inspected["allowed_actions"] == before["legal_actions"]


def test_current_workflow_service_rejects_action_after_live_placement_drift(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="review-run")
    backend.section = "rq"
    service = CurrentWorkflowService(app.conn, backend)
    view = service.authoritative_view(operation_id, schema=app._load_release(None).schema)
    assert view["legal_actions"] == []
    try:
        service.assert_action(operation_id, "approve", schema=app._load_release(None).schema)
    except DishRuleError as exc:
        assert exc.rule == "verification_placement_required"
    else:
        raise AssertionError("drifted operation unexpectedly allowed approval")
