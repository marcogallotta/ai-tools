from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "full_regression.py"
WORKFLOW = ROOT / ".github/workflows/full-regression.yml"
TRIAGE_SCHEMA = ROOT / "ci/schemas/full-regression-triage-v1.schema.json"
EVIDENCE_SCHEMA = ROOT / "ci/schemas/full-regression-evidence-v1.schema.json"
SHA, PREVIOUS_SHA, RESPONSIBLE_SHA = "a" * 40, "b" * 40, "c" * 40

spec = importlib.util.spec_from_file_location("full_regression", SCRIPT)
assert spec and spec.loader
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)


def _state(path: Path) -> None:
    fr.begin_run(
        output_dir=path,
        main_sha=SHA,
        previous_main_sha=PREVIOUS_SHA,
        previous_run_id="41",
        run_id="42",
        run_attempt="1",
        event="schedule",
        reason="",
        workflow_ref="repo/.github/workflows/full-regression.yml@refs/heads/main",
        started_at_epoch=time.time() - 61,
    )


def _lanes(path: Path, failed: str | None = None) -> None:
    for lane in fr.LANES:
        now = fr._utc_now()
        status = "failed" if lane == failed else "passed"
        fr.record_component(
            output_dir=path,
            kind="lane",
            name=lane,
            status=status,
            exit_code=1 if status == "failed" else 0,
            duration_seconds=1,
            started_at=now,
            finished_at=now,
            failure_kind="command_failed" if status == "failed" else None,
        )


def _miss() -> dict:
    return {
        "schema": fr.TRIAGE_SCHEMA,
        "full_regression_run_id": "42",
        "main_sha": SHA,
        "failure_id": "lane:browser-acceptance",
        "classification": "related regression",
        "analysis": "responsible change regressed a browser invariant",
        "responsible_change": {"pr_number": 123, "head_sha": RESPONSIBLE_SHA},
        "certification": {
            "plan_id": "plan-abc",
            "run_id": "9001",
            "candidate_sha": RESPONSIBLE_SHA,
        },
        "failing_invariant": "authenticated refresh preserves session",
        "failing_lane": "browser-acceptance",
        "selector_miss": True,
        "required_selector_correction": {
            "owner_action": "map session contracts to browser acceptance",
            "policy_update_paths": ["dish/test_selection/ownership.csv"],
            "representative_selector_regression": {
                "test_path": "dish/tests/test_selection/test_planner.py",
                "changed_path_class": "frontend session contract",
                "expected_lane": "browser-acceptance",
            },
        },
    }


def test_workflow_contract_is_independent_force_all_diagnostic_backstop():
    workflow = WORKFLOW.read_text()
    prefix = workflow.split("permissions:", 1)[0]
    assert "cron: '17 2 * * *'" in prefix and "workflow_dispatch:" in prefix
    assert "pull_request:" not in prefix and "push:" not in prefix
    assert "scripts/pr_gate.py" not in workflow
    for lane in fr.LANES:
        assert f"--lane {lane} --" in workflow
    assert workflow.count("run-lane") == 4
    assert "Adapter seam" in workflow
    assert workflow.index("Upload durable full-regression evidence") < workflow.index(
        "Enforce terminal full-regression result after evidence upload"
    )


def test_unchanged_success_dedupes_scheduled_only():
    runs = {"workflow_runs": [{"id": 100, "status": "completed", "conclusion": "success", "head_sha": SHA}]}
    scheduled = fr.decide_run(runs_payload=runs, main_sha=SHA, event="schedule", current_run_id="101")
    manual = fr.decide_run(runs_payload=runs, main_sha=SHA, event="workflow_dispatch", current_run_id="101")
    assert scheduled["should_run"] is False and scheduled["equivalent_success_run_id"] == "100"
    assert manual["should_run"] is True
    runs["workflow_runs"][0]["conclusion"] = "failure"
    assert fr.decide_run(runs_payload=runs, main_sha=SHA, event="schedule", current_run_id="101")["should_run"] is True


def test_failed_lane_records_but_does_not_fail_fast(tmp_path: Path):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "run-lane", "--output-dir", str(tmp_path), "--lane", "python-control-plane", "--", sys.executable, "-c", "import sys; sys.exit(7)"],
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 0
    record = json.loads((tmp_path / "components/lane-python-control-plane.json").read_text())
    assert record["status"] == "failed" and record["exit_code"] == 7


def test_evidence_records_exact_range_all_lanes_failure_and_timing(tmp_path: Path):
    _state(tmp_path)
    _lanes(tmp_path, failed="browser-acceptance")
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    assert evidence["main_sha"] == SHA and evidence["commit_range"] == f"{PREVIOUS_SHA}..{SHA}"
    assert set(evidence["lane_results"]) == set(fr.LANES)
    assert evidence["overall_result"] == "failed"
    assert evidence["estimated_billed_minutes"] >= 2
    assert evidence["triage"]["required_failure_ids"] == ["lane:browser-acceptance"]


def test_missing_required_lane_fails_closed(tmp_path: Path):
    _state(tmp_path)
    for lane in fr.LANES[:-1]:
        now = fr._utc_now()
        fr.record_component(output_dir=tmp_path, kind="lane", name=lane, status="passed", exit_code=0, duration_seconds=1, started_at=now, finished_at=now)
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    failure = next(item for item in evidence["failures"] if item["component"] == "browser-acceptance")
    assert failure["failure_kind"] == "missing_result"


def test_triage_has_exact_three_classes_and_selector_miss_exact_head_binding():
    schema = json.loads(TRIAGE_SCHEMA.read_text())
    assert schema["properties"]["classification"]["enum"] == list(fr.CLASSIFICATIONS)
    record = _miss()
    fr.validate_triage_record(record)
    record["certification"]["candidate_sha"] = "d" * 40
    with pytest.raises(fr.ContractError, match="must equal responsible_change.head_sha"):
        fr.validate_triage_record(record)


def test_selector_miss_binds_missed_lane_and_requires_policy_plus_regression():
    record = _miss()
    record["required_selector_correction"]["representative_selector_regression"]["expected_lane"] = "native-postgresql"
    with pytest.raises(fr.ContractError, match="expected_lane must equal failing_lane"):
        fr.validate_triage_record(record)
    record = _miss()
    with pytest.raises(fr.ContractError, match="policy"):
        fr.verify_selector_correction(triage=record, changed_paths=["dish/tests/test_selection/test_planner.py"])
    with pytest.raises(fr.ContractError, match="representative"):
        fr.verify_selector_correction(triage=record, changed_paths=["dish/test_selection/ownership.csv"])
    assert fr.verify_selector_correction(
        triage=record,
        changed_paths=["dish/test_selection/ownership.csv", "dish/tests/test_selection/test_planner.py"],
    )["valid"] is True


def test_every_failure_requires_exactly_one_valid_triage(tmp_path: Path):
    _state(tmp_path)
    _lanes(tmp_path, failed="browser-acceptance")
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    triage = tmp_path / "triage"
    triage.mkdir()
    assert fr.check_triage_coverage(evidence=evidence, triage_dir=triage)["complete"] is False
    (triage / "browser.json").write_text(json.dumps(_miss()))
    assert fr.check_triage_coverage(evidence=evidence, triage_dir=triage)["complete"] is True


def test_evidence_schema_requires_all_four_lanes():
    schema = json.loads(EVIDENCE_SCHEMA.read_text())
    assert set(schema["properties"]["lane_results"]["required"]) == set(fr.LANES)
