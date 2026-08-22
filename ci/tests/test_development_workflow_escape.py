from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "development_workflow_escape.py"
SCHEMA_PATH = ROOT / "ci" / "schemas" / "development-workflow-escape-v1.schema.json"
spec = importlib.util.spec_from_file_location("development_workflow_escape", MODULE_PATH)
escape = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = escape
spec.loader.exec_module(escape)

REPO = "marcogallotta/ai-tools"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def record(*, story="1001", root_class="test gap", duration=escape.UNKNOWN, owner="UNOWNED"):
    discovery = [f"asana:story:{story}", f"github:{REPO}:review:77@{HEAD}"]
    value = {
        "schema": escape.SCHEMA,
        "escape_id": escape.escape_identity(
            affected_change=f"github:{REPO}:pr:42@{HEAD}", discovery_evidence=discovery
        ),
        "observed_at": "2026-08-21T10:00:00Z",
        "affected_change": f"github:{REPO}:pr:42@{HEAD}",
        "observed_failure": "Required failure visibility was absent from the operator surface.",
        "discovery_evidence": discovery,
        "design_named_failure_mode": "NO",
        "marco_approval": {"status": escape.UNKNOWN, "decision_evidence": escape.UNKNOWN},
        "design_review": {"status": "MISSED", "detail": "Design Review did not name the missing operator surface."},
        "code_review_testing": {"status": "MISSED", "detail": "Review/tests did not exercise the visibility path."},
        "implementation_vs_reviewed_design": {"status": "ALIGNED", "detail": "Implementation matched the reviewed design."},
        "activation_runtime": {"status": escape.UNKNOWN, "detail": escape.UNKNOWN},
        "operator_impact": {"duration_ms": duration, "cost": escape.UNKNOWN},
        "root_class": root_class,
        "corrective_owner": owner,
        "owning_boundary": "Development Workflow",
        "telemetry_evidence": escape.UNKNOWN,
    }
    return value


def test_record_matches_published_schema_and_round_trips_comment():
    value = record()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)
    rendered = escape.render_comment(value)
    assert rendered.startswith(escape.MARKER + "\n")
    assert escape.parse_comment(rendered) == value


def test_identical_exact_source_evidence_cannot_double_count_escape():
    value = record()
    report = escape.fold_records([value, value])
    assert report["input_record_count"] == 2
    assert report["canonical_record_count"] == 1
    assert report["deduplicated_exact_evidence_count"] == 1
    assert report["active_root_classes"] == [{"root_class": "test gap", "recurrence_count": 1}]


def test_same_identity_with_conflicting_metadata_fails_closed():
    first = record()
    second = dict(first, observed_failure="Different claim for the same exact event evidence.")
    with pytest.raises(escape.EscapeRecordError, match="conflicting records"):
        escape.fold_records([first, second])


def test_distinct_recurrences_of_same_root_class_count_separately():
    first = record(story="1001")
    second = record(story="1002")
    assert first["escape_id"] != second["escape_id"]
    report = escape.fold_records([first, second])
    assert report["active_root_classes"] == [{"root_class": "test gap", "recurrence_count": 2}]
    assert {item["safeguard"] for item in report["repeatedly_failing_safeguards"]} == {
        "code-review-testing", "design-failure-mode-not-named", "design-review"
    }


def test_missing_or_ambiguous_required_source_identity_rejects_admission():
    missing = record()
    missing["discovery_evidence"] = []
    with pytest.raises(escape.EscapeRecordError, match="non-empty"):
        escape.validate_record(missing)

    ambiguous = record()
    ambiguous["affected_change"] = f"github:{REPO}:pr:42"
    with pytest.raises(escape.EscapeRecordError, match="exact GitHub"):
        escape.validate_record(ambiguous)


def test_unknown_schema_fields_fail_closed():
    value = record()
    value["priority"] = "P0"
    with pytest.raises(escape.EscapeRecordError, match="unknown fields"):
        escape.validate_record(value)


def test_missing_operator_duration_remains_unknown_not_zero():
    report = escape.fold_records([record()])
    assert report["operator_impact"]["exact_duration_count"] == 0
    assert report["operator_impact"]["unknown_duration_count"] == 1
    assert report["operator_impact"]["total_exact_duration_ms"] == 0
    assert report["evidence_operator_impact_trend"][0]["operator_duration_ms"] == escape.UNKNOWN


def test_authenticated_account_attribution_never_implies_marco_approval():
    value = record()
    value["marco_approval"] = {
        "status": "YES",
        "decision_evidence": "asana:actor:6385861764021",
    }
    with pytest.raises(escape.EscapeRecordError, match="account attribution is not decision provenance"):
        escape.validate_record(value)


def test_explicit_marco_decision_identity_is_accepted():
    value = record()
    value["marco_approval"] = {
        "status": "YES",
        "decision_evidence": "asana:decision:feedbeef1234@story:1217000000000001",
    }
    assert escape.validate_record(value)["marco_approval"]["status"] == "YES"


def test_corrective_owner_is_exact_task_or_unowned():
    owned = record(owner="asana:task:1217000000000001")
    report = escape.fold_records([owned, record(story="1002")])
    assert report["corrective_owner_state"]["owned_escape_count"] == 1
    assert report["corrective_owner_state"]["unowned_escape_count"] == 1
    assert report["corrective_owner_state"]["owners"][0]["task_state"] == escape.UNKNOWN

    invalid = record(owner="task sometime later")
    with pytest.raises(escape.EscapeRecordError, match="exact asana:task"):
        escape.validate_record(invalid)


def test_fold_output_is_byte_reproducible_for_same_ordered_canonical_set():
    records = [record(story="1001", duration=1200), record(story="1002", duration=900)]
    assert escape.report_bytes(records) == escape.report_bytes(records)


def test_input_digest_changes_when_canonical_record_set_changes():
    first = escape.fold_records([record(story="1001")])
    second = escape.fold_records([record(story="1001"), record(story="1002")])
    assert first["canonical_input_digest"] != second["canonical_input_digest"]


def test_v1_has_no_parent_note_write_or_control_plane_authority():
    report = escape.fold_records([record()])
    assert report["writes_performed"] is False
    assert report["parent_task_notes_mutated"] is False
    assert report["authority"] == "diagnostic_only"
    assert report["eligibility"] == escape.UNKNOWN
    assert report["routing_recommendation"] == escape.UNKNOWN
    assert report["priority_change"] == escape.UNKNOWN
    assert report["review_change"] == escape.UNKNOWN
    assert report["merge_change"] == escape.UNKNOWN
    assert report["human_gate_change"] == escape.UNKNOWN


def test_telemetry_degradation_never_changes_escape_truth_or_identity():
    without = record()
    with_telemetry = dict(without, telemetry_evidence=["telemetry:lifecycle-economics:event-42"])
    assert escape.validate_record(without)["escape_id"] == escape.validate_record(with_telemetry)["escape_id"]
    assert escape.fold_records([without])["canonical_record_count"] == 1
    assert escape.fold_records([with_telemetry])["canonical_record_count"] == 1
    invalid_identity = record()
    invalid_identity["discovery_evidence"].append("telemetry:lifecycle-economics:event-42")
    with pytest.raises(escape.EscapeRecordError, match="invalid exact evidence identity"):
        escape.validate_record(invalid_identity)


def test_malformed_marker_like_comment_fails_closed_and_non_records_are_ignored():
    assert escape.parse_comment("ordinary Asana comment") is None
    assert escape.parse_comment(f"design discussion mentions {escape.SCHEMA} but is not a record") is None
    with pytest.raises(escape.EscapeRecordError, match="first line"):
        escape.parse_comment("prefix\n" + escape.MARKER + "\n{}")


def test_later_diagnostics_do_not_stop_after_one_record_and_high_impact_is_deterministic():
    low = record(story="1001", duration=100)
    high = record(story="1002", duration=5000)
    report = escape.fold_records([low, high])
    assert report["canonical_record_count"] == 2
    assert report["recent_high_impact_examples"][0]["escape_id"] == high["escape_id"]


def test_equal_high_impact_prefers_more_recent_escape():
    older = record(story="1001", duration=5000)
    newer = record(story="1002", duration=5000)
    newer["observed_at"] = "2026-08-21T11:00:00Z"
    report = escape.fold_records([newer, older])
    assert report["recent_high_impact_examples"][0]["escape_id"] == newer["escape_id"]


def test_schema_enforces_unknown_detail_and_explicit_marco_decision():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())

    leaked_detail = record()
    leaked_detail["activation_runtime"] = {"status": escape.UNKNOWN, "detail": "guessed gap"}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(leaked_detail)

    account_claim = record()
    account_claim["marco_approval"] = {"status": "YES", "decision_evidence": escape.UNKNOWN}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(account_claim)


def test_repository_qualification_is_required_for_github_identity():
    value = record()
    value["discovery_evidence"] = [f"asana:story:1001", f"github:review:77@{HEAD}"]
    with pytest.raises(escape.EscapeRecordError, match="invalid exact evidence identity"):
        escape.validate_record(value)


def test_helper_has_no_scheduler_queue_database_or_remote_mutation_client():
    import ast

    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {"asana", "github", "httpx", "queue", "requests", "sched", "sqlalchemy", "sqlite3"}
    )


def test_cost_amount_rejects_noncanonical_numeric_spelling():
    value = record()
    value["operator_impact"]["cost"] = {"amount": "1e3", "currency": "EUR", "unit": "actual"}
    with pytest.raises(escape.EscapeRecordError, match="exact decimal string"):
        escape.validate_record(value)
