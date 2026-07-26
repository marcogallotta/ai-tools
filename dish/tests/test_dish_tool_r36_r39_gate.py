import dataclasses
import sqlite3
from pathlib import Path

import pytest

from dish_tool.database import initialize_database
from dish_tool.governed_diff import explicit_material_reasons, require_small_scope
from dish_tool.task_document import parse_task_document, validate_planning_brief, parse_planning_brief, finding_payload
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_step7_verification import TASK


def doc(text=TASK):
    return parse_task_document(text)


@pytest.mark.parametrize("replacement,reason", [
    ("1. Add 20 g salt and cook it.", "quantity"),
    ("1. Add wine and cook it.", "halal_or_safety"),
    ("1. Add pork and cook it.", "halal_or_safety"),
    ("1. Cook in an oven.", "equipment_or_method"),
    ("1. Mix 1:2 and cook it.", "ratio"),
])
def test_structured_materiality_catches_method_changes(replacement, reason):
    before = doc()
    after = doc(TASK.replace("1. Cook it.", replacement))
    assert reason in explicit_material_reasons(before, after)
    with pytest.raises(DishRuleError) as exc:
        require_small_scope(before, after)
    assert exc.value.rule == "large_correction_required"


def test_planning_finding_payload_is_actionable():
    brief = parse_planning_brief("""Dish candidate: X\nPurpose: X\nRole: main\nPriors: None\nLocks: None\nExemptions: None\nResearch emphasis: X\nDestination section: Reference (123)\n""")
    finding = validate_planning_brief(brief).findings[0]
    payload = finding_payload(finding)
    assert payload["message"] == "Destination section must be name — gid or a canonical defect marker"
    assert payload["location"] == "Destination section"


def test_schema_v16_and_audit_repair_table(tmp_path):
    conn = initialize_database(tmp_path / "db.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='verification_cycles_completed_fully_immutable_update'").fetchone()


def test_two_pass_reset_rejects_future_target_without_replacement():
    from dish_tool.step8 import _prove_reset
    before = doc()
    candidate = doc(TASK.replace("100 g test ingredient", "100 g test ingredient\nFuture target: 140 g test ingredient"))
    with pytest.raises(DishRuleError) as exc:
        _prove_reset(before, candidate, "scope", "100 g test ingredient", "140 g test ingredient")
    assert exc.value.rule == "two_pass_reset_not_applied"


def test_start_returns_environment_specific_runtime_context(tmp_path, monkeypatch):
    from tests.test_dish_tool_step7_verification import Backend
    from dish_tool.commands import DishApplication
    from dish_tool.models import ResolvedRelease
    from dish_tool.database import initialize_database
    backend = Backend()
    honest = tmp_path / "honest"; honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text("# Verification\n")
    def release(role=None):
        return ResolvedRelease(version="1.0.6", commit="", root=honest,
            protocols={} if role is None else {role: f"{role} protocol"},
            manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}",
            migration_metadata={}, requested_protocol_role=role)
    app = DishApplication(initialize_database(tmp_path / "dish.db"), backend, release_loader=release)
    result = app.execute("start", agent="gpt", task_gid="t", kind="initial", run_id="constructor")
    assert result["ok"]
    context = result["data"]["runtime_context"]
    assert context["destination_format"] == "<section name> — <section gid>"
    assert context["research_queue"]["gid"] == "rq"
    assert context["verification_queue"]["gid"] == "vq"


def test_completed_evidence_and_consumed_authorization_are_immutable(tmp_path):
    from tests.test_dish_tool_step7_verification import make_app
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="verify-run")
    approved = app.execute("approve", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True,
        provenance_complete=True, run_id="verify-run")
    assert approved["ok"]
    cycle = app.conn.execute("SELECT * FROM verification_cycles WHERE operation_id=? AND outcome='approved'", (operation_id,)).fetchone()
    attempt = app.conn.execute("SELECT * FROM write_attempts WHERE operation_id=? AND outcome='confirmed' ORDER BY started_at DESC LIMIT 1", (operation_id,)).fetchone()
    for sql, args in [
        ("UPDATE verification_cycles SET protocol_text='tampered' WHERE cycle_id=?", (cycle["cycle_id"],)),
        ("UPDATE verification_cycles SET correction_class='large' WHERE cycle_id=?", (cycle["cycle_id"],)),
        ("UPDATE verification_cycles SET cycle_number=99 WHERE cycle_id=?", (cycle["cycle_id"],)),
        ("UPDATE write_attempts SET started_at='tampered' WHERE attempt_id=?", (attempt["attempt_id"],)),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            app.conn.execute(sql, args)
    app.conn.execute("""INSERT INTO marco_authorizations(
        authorization_id,task_gid,operation_id,field_name,before_json,after_json,reason,
        actor_run_id,created_at,consumed_at,reserved_by_operation_id,reserved_at,consumed_identity
    ) VALUES('auth-immutable','t',?,'Locks','\"a\"','\"b\"','test','marco','now','now',?,'now',?)""",
        (operation_id, operation_id, cycle["signed_identity"]))
    with pytest.raises(sqlite3.IntegrityError):
        app.conn.execute("UPDATE marco_authorizations SET reason='changed' WHERE authorization_id='auth-immutable'")
    with pytest.raises(sqlite3.IntegrityError):
        app.conn.execute("DELETE FROM marco_authorizations WHERE authorization_id='auth-immutable'")


def test_audit_repair_fallback_is_imported_and_completed(monkeypatch, tmp_path):
    import dish_tool.commands as commands
    from tests.test_dish_tool_step7_verification import make_app
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="verify-run")
    original_audit = commands.record_audit
    original_repair = commands.record_command_audit_repair
    monkeypatch.setattr(commands, "record_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    monkeypatch.setattr(commands, "record_command_audit_repair", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("repair insert down")))
    result = app.execute("approve", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"], semantic_review_complete=True,
        provenance_complete=True, run_id="verify-run")
    assert result["ok"]
    assert result["data"]["audit_repair_required"] is True
    assert result["data"]["audit_repair_persisted_in_database"] is False
    monkeypatch.setattr(commands, "record_audit", original_audit)
    monkeypatch.setattr(commands, "record_command_audit_repair", original_repair)
    app.execute("inspect", agent="gpt", submission_id=operation_id)
    row = app.conn.execute("SELECT operation_id,repaired_at FROM command_audit_repairs WHERE repair_id=?", (result["data"]["audit_repair_id"],)).fetchone()
    assert row["operation_id"] == operation_id
    assert row["repaired_at"] is not None
