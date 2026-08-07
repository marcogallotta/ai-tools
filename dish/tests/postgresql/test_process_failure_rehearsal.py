from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from dish_pg import process_failure_rehearsal as rehearsal
from tests.support.postgresql.certification import discover_native_postgresql_inventory

DISH_ROOT = Path(__file__).resolve().parents[2]


def _report_hash(payload: dict) -> str:
    copy = dict(payload)
    expected = copy.pop("report_sha256")
    actual = hashlib.sha256(rehearsal._canonical_bytes(copy)).hexdigest()
    assert actual == expected
    return expected



@pytest.mark.parametrize(
    "name",
    ["dish_section1_process_failure_test", "dish_custom_42_test"],
)
def test_process_failure_rehearsal_accepts_only_disposable_database_names(name: str) -> None:
    assert rehearsal._safe_database_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["dish", "dish_stage_a", "production_test", "dish_production_test", "dish_prod_test"],
)
def test_process_failure_rehearsal_rejects_unsafe_database_names(name: str) -> None:
    with pytest.raises(rehearsal.RehearsalConfigurationError):
        rehearsal._safe_database_name(name)


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_process_failure_rehearsal_rejects_nonfinite_or_nonpositive_timeouts(value: float) -> None:
    with pytest.raises(rehearsal.RehearsalConfigurationError):
        rehearsal._safe_timeout(value, name="test timeout")


def test_base_report_never_claims_scripted_completion_with_unimplemented_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rehearsal,
        "NOT_IMPLEMENTED_SCENARIOS",
        ("reconciliation loss after durable run creation",),
    )
    args = rehearsal.build_parser().parse_args(
        [
            "--output",
            str(tmp_path / "report.json"),
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ]
    )

    report = rehearsal._base_report(args, run_evidence=tmp_path / "evidence" / "run")

    assert report["delivery_classification"] == "incomplete_section1_scripted_package"
    assert report["section1_scripted_package_complete"] is False
    assert report["section1_implementation_status"] == "incomplete"
    assert report["section1_implemented"] is False
    assert report["section1_certified"] is False
    assert report["not_implemented_scenarios"] == [
        "reconciliation loss after durable run creation"
    ]
    assert report["implemented_scenario_count"] == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["required_scenario_count"] == len(rehearsal.PROCESS_TEST_INVENTORY) + 1


def test_process_failure_rehearsal_distinguishes_complete_scripts_from_blocked_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(rehearsal.shutil, "which", lambda _name: None)

    result = rehearsal.main(
        [
            "--output",
            str(output),
            "--evidence-dir",
            str(evidence),
            "--compose-project",
            "dish-section1-source-test",
        ]
    )

    assert result == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert report["ok"] is False
    assert report["delivery_classification"] == "complete_section1_scripted_package"
    assert report["section1_scripted_package_complete"] is True
    assert report["section1_implementation_status"] == "complete"
    assert report["section1_implemented"] is True
    assert report["section1_certified"] is False
    assert report["not_implemented_scenarios"] == []
    assert report["implemented_scenario_count"] == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["required_scenario_count"] == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["command_process_requirements_blocked"] is False
    assert report["remaining_native_scenarios_blocked"] is True
    assert report["process_failure_rehearsal_status"] == (
        "blocked_by_unavailable_native_infrastructure"
    )
    assert report["process_failure_native_evidence_validated"] is False
    assert report["evidence_validation"]["ok"] is False
    assert report["evidence_validation"]["status"] == (
        "not_run_blocked_by_unavailable_native_infrastructure"
    )
    assert report["postgresql_identity"] is None
    assert report["test_inventory"] == list(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["test_summary"]["passed_count"] == 0
    assert report["test_summary"]["not_run_count"] == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["production_asana_touched"] is False
    process_requirements = report["requirements"][: len(rehearsal.PROCESS_TEST_INVENTORY)]
    assert all(item["implementation_status"] == "implemented" for item in process_requirements)
    assert all(
        item["status"] == "blocked_by_unavailable_native_infrastructure"
        for item in process_requirements
    )
    assert report["native_execution_blocked_scenarios"] == [
        rehearsal.NODE_REQUIREMENTS[nodeid] for nodeid in rehearsal.PROCESS_TEST_INVENTORY
    ]
    statuses = {item["requirement"]: item["status"] for item in report["requirements"]}
    assert statuses["long_running_projection_supervision_and_restart"] == (
        "blocked_by_unavailable_native_infrastructure"
    )
    assert statuses["reconciliation_worker_supervision_and_restart"] == (
        "blocked_by_unavailable_native_infrastructure"
    )
    assert statuses["reconciliation_loss_after_durable_run_creation"] == (
        "blocked_by_unavailable_native_infrastructure"
    )
    assert statuses["reconciliation_loss_after_partially_recorded_corpus"] == (
        "blocked_by_unavailable_native_infrastructure"
    )
    assert statuses["deadlock_and_serialization_policy"] == "not_exercised_no_defined_policy"
    _report_hash(report)


def test_section1_runner_uses_literal_nodes_without_governed_lane_selector(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "section1.xml"
    command = rehearsal._section1_pytest_command(python=sys.executable, junit=junit)

    assert command[:4] == [sys.executable, "-m", "pytest", "--postgresql"]
    assert "--native-postgresql" not in command
    assert command[-len(rehearsal.PROCESS_TEST_INVENTORY) :] == list(
        rehearsal.PROCESS_TEST_INVENTORY
    )


def test_requirement_status_distinguishes_not_run_from_infrastructure_blocked() -> None:
    not_run = rehearsal._requirements_from_cases({})
    blocked = rehearsal._requirements_from_cases(
        {}, unavailable_reason="native PostgreSQL service unavailable"
    )

    assert all(
        item["implementation_status"] == "implemented"
        for item in not_run[: len(rehearsal.PROCESS_TEST_INVENTORY)]
    )
    assert all(
        item["status"] == "not_run"
        for item in not_run[: len(rehearsal.PROCESS_TEST_INVENTORY)]
    )
    assert all(
        item["status"] == "blocked_by_unavailable_native_infrastructure"
        for item in blocked[: len(rehearsal.PROCESS_TEST_INVENTORY)]
    )
    assert all(
        item["detail"] == "native PostgreSQL service unavailable"
        for item in blocked[: len(rehearsal.PROCESS_TEST_INVENTORY)]
    )


def test_process_failure_inventory_is_literal_process_owned_and_scenario_complete() -> None:
    assert rehearsal.NOT_IMPLEMENTED_SCENARIOS == ()
    assert len(rehearsal.PROCESS_TEST_INVENTORY) == 14
    assert len(rehearsal.PROCESS_TEST_INVENTORY) == len(set(rehearsal.PROCESS_TEST_INVENTORY))
    assert set(rehearsal.NODE_REQUIREMENTS) == set(rehearsal.PROCESS_TEST_INVENTORY)
    assert set(rehearsal.NODE_SCENARIOS) == set(rehearsal.PROCESS_TEST_INVENTORY)
    assert len(set(rehearsal.NODE_SCENARIOS.values())) == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert all(
        node.startswith("tests/postgresql/native/test_process_failure_")
        for node in rehearsal.PROCESS_TEST_INVENTORY
    )
    assert all("::test_" in node for node in rehearsal.PROCESS_TEST_INVENTORY)
    assert set(rehearsal.PROCESS_TEST_INVENTORY).issubset(
        discover_native_postgresql_inventory(DISH_ROOT)
    )


def test_process_barriers_do_not_use_sleep_as_synchronization() -> None:
    paths = [
        DISH_ROOT / "dish_pg/process_failure_rehearsal.py",
        DISH_ROOT / "tests/support/postgresql/process_failure.py",
        DISH_ROOT / "tests/support/postgresql/process_failure_adapter.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_command.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_projection.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_takeover.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_supervision.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_reconciliation.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_disconnect.py",
    ]
    forbidden = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if "time.sleep(" in text or ".sleep(" in text:
            forbidden.append(str(path))
    assert forbidden == []
