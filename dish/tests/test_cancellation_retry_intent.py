import json
from pathlib import Path

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.database import confirm_task_content, create_operation, declare_operation_step
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from dish_tool.recovery import begin_movement_attempt, begin_operation_write_attempt


class Backend:
    def __init__(self, task_gid, title, notes, section="research"):
        self.task_gid = task_gid
        self.title = title
        self.notes = notes
        self.section = section

    def read_task(self, task_gid):
        return {
            "gid": task_gid,
            "name": self.title,
            "notes": self.notes,
            "completed": False,
            "modified_at": "fixture",
            "memberships": [{"project": {"gid": "1215089183018968"}, "section": {"gid": self.section}}],
        }

    def list_sections(self, project_gid):
        return [
            {"gid": self.section, "name": "Research Queue"},
            {"gid": "verification-queue", "name": "Verification Queue"},
            {"gid": "sourcing", "name": "Sourcing"},
            {"gid": "reference", "name": "Reference"},
        ]


def _operation(tmp_path: Path):
    conn = initialize_database(tmp_path / "db.sqlite")
    task_gid = "task"
    baseline = confirm_task_content(conn, task_gid=task_gid, title="Dish", notes="baseline", schema_version="2")
    op = create_operation(
        conn,
        task_gid=task_gid,
        operation_kind="initial",
        expected_identity=baseline.digest,
        schema_version="2",
        actors=OperationActors(editor_agent="gpt", run_id="run-a"),
    )
    return conn, op, baseline


def test_retry_step_intent_must_match_exactly(tmp_path):
    conn, op, _ = _operation(tmp_path)
    declare_operation_step(conn, op["operation_id"], "candidate_write", {"title": "A", "notes": "one"})
    declare_operation_step(conn, op["operation_id"], "candidate_write", {"title": "A", "notes": "one"})
    with pytest.raises(DishRuleError) as exc:
        declare_operation_step(conn, op["operation_id"], "candidate_write", {"title": "B", "notes": "two"})
    assert exc.value.rule == "operation_step_intent_mismatch"


def test_new_attempt_is_blocked_while_older_attempt_is_unresolved(tmp_path):
    conn, op, baseline = _operation(tmp_path)
    begin_operation_write_attempt(
        conn,
        operation_id=op["operation_id"],
        expected_identity=baseline.digest,
        intended_identity="new",
        intended_title="Dish",
        intended_notes="new",
        schema_version="2",
    )
    with pytest.raises(DishRuleError) as exc:
        begin_operation_write_attempt(
            conn,
            operation_id=op["operation_id"],
            expected_identity=baseline.digest,
            intended_identity="newer",
            intended_title="Dish",
            intended_notes="newer",
            schema_version="2",
        )
    assert exc.value.rule == "unresolved_write_attempt"

    begin_movement_attempt(conn, operation_id=op["operation_id"], expected_section_gid="research", intended_section_gid="verification")
    with pytest.raises(DishRuleError) as exc:
        begin_movement_attempt(conn, operation_id=op["operation_id"], expected_section_gid="research", intended_section_gid="verification")
    assert exc.value.rule == "unresolved_movement_attempt"


def test_cancellation_rejects_confirmed_intermediate_mutation(tmp_path):
    conn, op, baseline = _operation(tmp_path)
    confirm_task_content(conn, task_gid=op["task_gid"], title="Dish", notes="baseline", schema_version="2", operation_id=op["operation_id"], boundary="content_write")
    conn.execute(
        """INSERT INTO write_attempts(
            attempt_id, operation_id, expected_identity, intended_identity, outcome,
            started_at, finished_at, purpose, intended_title, intended_notes,
            schema_version, confirmed_content_version_id
        ) VALUES('attempt',?,?,?,?,?,?,?,?,?,?,?)""",
        (
            op["operation_id"], baseline.digest, baseline.digest, "confirmed",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "content_write",
            "Dish", "baseline", "2",
            conn.execute("SELECT content_version_id FROM content_versions WHERE task_gid=? ORDER BY rowid DESC LIMIT 1", (op["task_gid"],)).fetchone()[0],
        ),
    )
    app = DishAdminApplication(conn, backend=Backend(op["task_gid"], "Dish", "baseline"))
    result = app.execute("discard", submission_id=op["operation_id"], reason="stop")
    assert result["ok"] is False
    assert result["errors"][-1]["rule"] == "operation_cancel_applied_effects"
    assert conn.execute("SELECT status FROM operations WHERE operation_id=?", (op["operation_id"],)).fetchone()[0] == "open"


def _bounded_recovery_contract() -> str:
    control_plane = Path(__file__).resolve().parents[2] / "OPERATOR_CONTROL_PLANE.md"
    text = control_plane.read_text()
    start = text.index("### Bounded autonomous recovery")
    end = text.index("\n## TRUE READY dispatch queue", start)
    return text[start:end]


def test_bounded_recovery_is_continuation_without_new_authority() -> None:
    contract = _bounded_recovery_contract()
    assert "already-authorized objective" in contract
    assert "continuation problem, not a new operator decision" in contract
    assert "creates no source authority, role composition, scheduler, database, queue, service, or control plane" in contract
    assert "mapped standing role and current host authority remain controlling" in contract


def test_bounded_recovery_retries_only_supported_causal_same_operation() -> None:
    contract = _bounded_recovery_contract()
    assert "environmental, prerequisite, transient" in contract
    assert "existing supported operation" in contract
    assert "smallest supported causal remediation" in contract
    assert "immediately rerun the same failed operation" in contract
    assert "on PASS, continue the already-authorized objective without interrupting Marco" in contract


def test_bounded_recovery_requires_checkpoint_for_mutable_remediation() -> None:
    contract = _bounded_recovery_contract()
    assert "capture or reuse the supported known-good pre-state/checkpoint" in contract
    assert "reversible or bounded" in contract


def test_bounded_recovery_reconciles_ambiguous_write_before_replay() -> None:
    contract = _bounded_recovery_contract()
    assert "perform authoritative readback/reconciliation before replay" in contract
    assert "If the intended effect is proven present, resume from observed state and **do not replay**" in contract
    assert "If absence is proven and replay is safe/idempotent, one retry may proceed" in contract
    assert "If the outcome cannot be established or replay could duplicate/compound the mutation, fail closed" in contract


def test_bounded_recovery_transient_reads_share_total_budget() -> None:
    contract = _bounded_recovery_contract()
    assert "Read-only/idempotent operations may use normal bounded transient retry/backoff" in contract
    assert "same total recovery budget" in contract


def test_bounded_recovery_has_per_class_and_total_hard_stops() -> None:
    contract = _bounded_recovery_contract()
    assert "at most one diagnosed remediation plus one immediate retry" in contract
    assert "never repeat the same unresolved remediation loop" in contract
    assert "at most **two distinct automatic recovery cycles**" in contract
    assert "Exhaustion stops deterministically" in contract
    assert "Do not evade either bound by relabeling an unresolved failure" in contract


def test_bounded_recovery_new_class_requires_forward_progress() -> None:
    contract = _bounded_recovery_contract()
    assert "genuinely distinct newly exposed failure class" in contract
    assert "prior cycle made demonstrable forward progress" in contract
    assert "alleged next class is not genuinely new or prior recovery made no forward progress" in contract


def test_bounded_recovery_stops_at_moved_or_consequential_boundaries() -> None:
    contract = _bounded_recovery_contract()
    assert "candidate/head/target moved" in contract
    assert "diagnosis admits materially different fixes" in contract
    assert "no new credentials/login" in contract
    assert "destructive operation, production mutation" in contract
    assert "security/product/architecture/authority" in contract
    assert "consequential human-decision boundaries" in contract


def test_bounded_recovery_does_not_grant_source_mutation_authority() -> None:
    contract = _bounded_recovery_contract()
    assert "this section never creates source mutation authority" in contract
    assert "a non-Implementation role may not use recovery as a route into source Implementation" in contract


def test_bounded_recovery_attempt_memory_reuses_existing_durable_state() -> None:
    contract = _bounded_recovery_contract()
    assert "Persist only the attempt/failure information actually required" in contract
    assert "using existing task/PR/local durable state" in contract
    assert "Never add a retry database or alternate lifecycle authority" in contract
