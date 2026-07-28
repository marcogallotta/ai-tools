import json

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.backend import AsanaBackend
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import process_command_audit_repairs
from tests.test_dish_tool_step7_verification import make_app


def _review(app, operation_id, *, run="review"):
    result = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id=run, independence_attestation="independent")
    assert result["ok"]
    inspected = app.execute("inspect", agent="codex", submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result


def test_post_success_view_failure_preserves_approval_success(monkeypatch, tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review = _review(app, operation_id)
    service = app.operation_service.current
    original = service.authoritative_view
    calls = {"count": 0}

    def fail_only_after_effect(op_id, *, schema=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("post-effect view unavailable")
        return original(op_id, schema=schema)

    monkeypatch.setattr(service, "authoritative_view", fail_only_after_effect)
    result = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id,
        correction="none", reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="review",
        independence_attestation="independent",
    )
    assert result["ok"]
    assert result["allowed_actions"] == []
    assert result["data"]["view_refresh_required"] is True
    assert result["data"]["view_refresh_error"]["type"] == "RuntimeError"
    row = app.conn.execute(
        "SELECT phase,signoff_completed_at FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert row["phase"] == "await_submission"
    assert row["signoff_completed_at"] is not None


def test_admin_audit_failure_uses_same_repair_lifecycle(monkeypatch, tmp_path):
    import dish_tool.invocation_audit as invocation_audit

    app, backend, operation_id, _ = make_app(tmp_path)
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release("verification"))
    original = invocation_audit.record_audit
    monkeypatch.setattr(
        invocation_audit,
        "record_audit",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    result = admin.execute(
        "authorize-governed-change", submission_id=operation_id,
        field="Locks", before="Keep crisp", after="Keep very crisp",
        reason="Marco authorised stronger crispness", run_id="marco",
    )
    assert result["ok"]
    assert result["data"]["audit_repair_required"] is True
    assert result["data"]["audit_repair_persisted_in_database"] is True
    repair = app.conn.execute(
        "SELECT * FROM command_audit_repairs WHERE repair_id=?",
        (result["data"]["audit_repair_id"],),
    ).fetchone()
    assert repair["operation_id"] == operation_id
    assert repair["command"] == "dish-admin.authorize-governed-change"

    monkeypatch.setattr(invocation_audit, "record_audit", original)
    assert process_command_audit_repairs(app.conn) == 1
    event = app.conn.execute(
        "SELECT event_type,operation_id,result_code,result_ok,details FROM audit_events WHERE event_type=? ORDER BY rowid DESC LIMIT 1",
        ("dish-admin.authorize-governed-change",),
    ).fetchone()
    assert tuple(event[:4]) == ("dish-admin.authorize-governed-change", operation_id, "OK", 1)
    details = json.loads(event["details"])
    assert details["actor_role"] == "marco"
    assert details["repaired_from"] == repair["repair_id"]


def test_total_admin_audit_outage_never_reverses_committed_authorization(monkeypatch, tmp_path):
    import dish_tool.invocation_audit as invocation_audit

    app, backend, operation_id, _ = make_app(tmp_path)
    admin = DishAdminApplication(app.conn, backend=backend, release_loader=lambda: app._load_release("verification"))
    monkeypatch.setattr(
        invocation_audit,
        "record_audit",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    monkeypatch.setattr(
        invocation_audit,
        "record_command_audit_repair",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("repair table unavailable")),
    )
    monkeypatch.setattr(
        invocation_audit,
        "_write_emergency_repair",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("filesystem unavailable")),
    )
    result = admin.execute(
        "authorize-governed-change", submission_id=operation_id,
        field="Locks", before="Keep crisp", after="Keep very crisp",
        reason="Marco authorised stronger crispness", run_id="marco",
    )
    assert result["ok"]
    assert result["data"]["audit_repair_required"] is True
    assert result["data"]["audit_repair_persisted_in_database"] is False
    assert result["data"]["audit_repair_persisted_in_fallback"] is False
    assert app.conn.execute(
        "SELECT 1 FROM marco_authorizations WHERE operation_id=? AND field_name='Locks'",
        (operation_id,),
    ).fetchone() is not None


def test_backend_movement_confirms_cooking_membership_not_first_membership(monkeypatch):
    backend = AsanaBackend(api_client=object())
    other = {"project": {"gid": "other-project"}, "section": {"gid": "other-section"}}
    reads = iter(
        [
            {
                "gid": "t", "name": "Dish", "notes": "N",
                "memberships": [
                    other,
                    {"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": "old"}},
                ],
            },
            {
                "gid": "t", "name": "Dish", "notes": "N",
                "memberships": [
                    other,
                    {"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": "new"}},
                ],
            },
        ]
    )
    monkeypatch.setattr(backend, "read_task", lambda _gid: next(reads))
    monkeypatch.setattr(backend, "call", lambda *args, **kwargs: {})
    backend.move_task_to_section(task_gid="t", section_gid="new")
