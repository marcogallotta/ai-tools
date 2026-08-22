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
sys.path.insert(0, str(ROOT / "scripts"))
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


def _miss(
    *,
    failure_id: str = "lane:browser-acceptance:test-browser:deadbeefdeadbeef",
    invariant: str = "authenticated refresh preserves session",
    causal_fingerprint: str = "ci-cause-v1:" + "d" * 32,
) -> dict:
    return {
        "schema": fr.TRIAGE_SCHEMA,
        "full_regression_run_id": "42",
        "main_sha": SHA,
        "failure_id": failure_id,
        "causal_fingerprint": causal_fingerprint,
        "classification": "related regression",
        "analysis": "responsible change regressed a browser invariant",
        "responsible_change": {"pr_number": 123, "head_sha": RESPONSIBLE_SHA},
        "certification": {
            "plan_id": "plan-abc",
            "run_id": "9001",
            "candidate_sha": RESPONSIBLE_SHA,
        },
        "failing_invariant": invariant,
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
    assert workflow.count("run-lane") == len(fr.LANES)
    assert "Adapter seam" in workflow
    assert workflow.index("Upload durable full-regression evidence") < workflow.index(
        "Enforce terminal full-regression result after evidence upload"
    )


def test_native_postgresql_skip_waivers_use_shared_registry_serializer():
    workflow = WORKFLOW.read_text()

    assert "python3 scripts/native_postgresql_waivers.py args" in workflow
    assert "waiver_rc=0" in workflow
    assert 'if [ "$waiver_rc" -ne 0 ]' in workflow
    assert "mapfile -t native_waiver_args" in workflow
    assert '"${native_waiver_args[@]}"' in workflow
    assert "--waive-skip" not in workflow
    assert "1217428310522281" not in workflow


def test_workflow_runs_governed_pglite_exactly_once_before_later_groups():
    workflow = WORKFLOW.read_text()
    install = "Install PGlite Node dependencies"
    pglite_lane = "Run governed PGlite group (harness:pglite-nested-collection)"
    python_lane = "Run Python/control-plane group"
    assert install in workflow
    assert "--phase pglite-node-dependencies --" in workflow
    assert "cd dish/tests/postgresql/pglite" in workflow
    assert "npm ci --no-audit --no-fund" in workflow
    assert workflow.count("--lane pglite --") == 1
    assert workflow.count("dish/scripts/dish-pg-pglite") == 1
    assert workflow.count("harness:pglite-nested-collection") == 1
    assert workflow.count("collect-pglite-report") == 1
    assert workflow.index(install) < workflow.index(pglite_lane) < workflow.index(python_lane)
    assert workflow.index(pglite_lane) < workflow.index("Run frontend static/tooling group")
    assert workflow.index(pglite_lane) < workflow.index("Run native PostgreSQL group")
    assert workflow.index(pglite_lane) < workflow.index("Run browser acceptance group")


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
        [sys.executable, str(SCRIPT), "run-lane", "--output-dir", str(tmp_path), "--lane", "pglite", "--", sys.executable, "-c", "import sys; sys.exit(7)"],
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode == 0
    record = json.loads((tmp_path / "components/lane-pglite.json").read_text())
    assert record["status"] == "failed" and record["exit_code"] == 7


def test_evidence_records_exact_range_all_lanes_failure_and_timing(tmp_path: Path):
    _state(tmp_path)
    _lanes(tmp_path, failed="browser-acceptance")
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    assert evidence["main_sha"] == SHA and evidence["commit_range"] == f"{PREVIOUS_SHA}..{SHA}"
    assert set(evidence["lane_results"]) == set(fr.LANES)
    assert evidence["overall_result"] == "failed"
    assert evidence["estimated_billed_minutes"] >= 2
    required = evidence["triage"]["required_failure_ids"]
    assert len(required) == 1
    assert required[0].startswith("lane:browser-acceptance:lane-command:")
    assert evidence["lane_results"]["browser-acceptance"]["failure_ids"] == required
    failure = evidence["failures"][0]
    assert failure["causal_fingerprint"] is None
    assert failure["causal_identity"] is None


def test_causal_fingerprint_ignores_occurrence_sha_and_separates_distinct_causes(tmp_path: Path):
    first = fr.record_failure(
        output_dir=tmp_path / "first", kind="lane", component="python-control-plane",
        source="pytest", invariant="tests/test_policy.py::test_owner", failure_kind="test_failure",
        detail="expected 5 got 8; run id 40",
    )
    repeated = fr.record_failure(
        output_dir=tmp_path / "repeated", kind="lane", component="python-control-plane",
        source="pytest", invariant="tests/test_policy.py::test_owner", failure_kind="test_failure",
        detail="expected 5 got 8; run id 41",
    )
    distinct = fr.record_failure(
        output_dir=tmp_path / "distinct", kind="lane", component="python-control-plane",
        source="pytest", invariant="tests/test_policy.py::test_owner", failure_kind="test_failure",
        detail="database connection refused",
    )
    assert first["causal_fingerprint"] == repeated["causal_fingerprint"]
    assert first["causal_fingerprint"] != distinct["causal_fingerprint"]


def test_weak_fallback_evidence_requires_ambiguous_triage(tmp_path: Path):
    _state(tmp_path)
    _lanes(tmp_path, failed="browser-acceptance")
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    failure = evidence["failures"][0]
    assert failure["causal_fingerprint"] is None
    record = {
        "schema": fr.TRIAGE_SCHEMA,
        "full_regression_run_id": "42",
        "main_sha": SHA,
        "failure_id": failure["failure_id"],
        "causal_fingerprint": None,
        "classification": "unrelated baseline",
        "analysis": "coarse fallback only",
    }
    with pytest.raises(fr.ContractError, match="must remain ambiguous"):
        fr.validate_triage_record(record, evidence)
    record["classification"] = "ambiguous"
    fr.validate_triage_record(record, evidence)


def test_named_failure_kind_without_material_detail_remains_ambiguous(tmp_path: Path):
    failure = fr.record_failure(
        output_dir=tmp_path,
        kind="lane",
        component="native-postgresql",
        source="pytest",
        invariant="tests/test_native.py::test_locking",
        failure_kind="failed",
        detail=None,
    )
    assert failure["causal_fingerprint"] is None
    assert failure["causal_identity"] is None


def test_missing_required_lane_fails_closed(tmp_path: Path):
    _state(tmp_path)
    for lane in fr.LANES[:-1]:
        now = fr._utc_now()
        fr.record_component(output_dir=tmp_path, kind="lane", name=lane, status="passed", exit_code=0, duration_seconds=1, started_at=now, finished_at=now)
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    failure = next(item for item in evidence["failures"] if item["component"] == "browser-acceptance")
    assert failure["failure_kind"] == "missing_result"
    assert failure["invariant"] == "required lane result exists"


def test_triage_has_exact_three_classes_and_selector_miss_exact_head_binding():
    schema = json.loads(TRIAGE_SCHEMA.read_text())
    assert schema["properties"]["classification"]["enum"] == list(fr.CLASSIFICATIONS)
    assert schema["properties"]["failing_lane"]["enum"] == list(fr.LANES)
    assert schema["properties"]["required_selector_correction"]["properties"]["representative_selector_regression"]["properties"]["expected_lane"]["enum"] == list(fr.LANES)
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


def test_multiple_failures_in_one_lane_are_independently_triageable(tmp_path: Path):
    _state(tmp_path)
    _lanes(tmp_path, failed="browser-acceptance")
    junit = tmp_path / "browser.xml"
    junit.write_text(
        """<testsuite tests="2" failures="1" errors="1">
        <testcase classname="browser.auth" name="test_refresh"><failure message="session lost"/></testcase>
        <testcase classname="browser.detail" name="test_history"><error message="fixture unavailable"/></testcase>
        </testsuite>"""
    )
    failures = fr.collect_junit_failures(
        output_dir=tmp_path, lane="browser-acceptance", source="acceptance-pytest",
        junit_path=junit, command_exit=1,
    )
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    assert len(failures) == 2
    assert len({item["failure_id"] for item in failures}) == 2
    assert set(evidence["lane_results"]["browser-acceptance"]["failure_ids"]) == {
        item["failure_id"] for item in failures
    }
    assert not any(
        item["source"] == "lane-command" and item["component"] == "browser-acceptance"
        for item in evidence["failures"]
    )

    first, second = sorted(failures, key=lambda item: item["invariant"])
    triage = tmp_path / "triage"
    triage.mkdir()
    related = _miss(
        failure_id=first["failure_id"], invariant=first["invariant"],
        causal_fingerprint=first["causal_fingerprint"],
    )
    baseline = {
        "schema": fr.TRIAGE_SCHEMA,
        "full_regression_run_id": "42",
        "main_sha": SHA,
        "failure_id": second["failure_id"],
        "causal_fingerprint": second["causal_fingerprint"],
        "classification": "unrelated baseline",
        "analysis": "failure reproduced before the responsible range",
    }
    (triage / "related.json").write_text(json.dumps(related))
    assert fr.check_triage_coverage(evidence=evidence, triage_dir=triage)["complete"] is False
    (triage / "baseline.json").write_text(json.dumps(baseline))
    coverage = fr.check_triage_coverage(evidence=evidence, triage_dir=triage)
    assert coverage["complete"] is True
    assert set(coverage["classified_failure_ids"]) == {first["failure_id"], second["failure_id"]}


def test_triage_related_failure_must_match_evidence_lane_and_invariant(tmp_path: Path):
    _state(tmp_path)
    _lanes(tmp_path, failed="browser-acceptance")
    failure = fr.record_failure(
        output_dir=tmp_path, kind="lane", component="browser-acceptance",
        source="browser-harness", invariant="fixture board renders", failure_kind="assertion_failure",
        detail="expected fixture board to render",
    )
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    record = _miss(
        failure_id=failure["failure_id"], invariant="wrong invariant",
        causal_fingerprint=failure["causal_fingerprint"],
    )
    with pytest.raises(fr.ContractError, match="failing_invariant does not match"):
        fr.validate_triage_record(record, evidence)


def test_pglite_report_preserves_assertion_infrastructure_and_junit_evidence(tmp_path: Path):
    primary_junit = tmp_path / "primary.junit.xml"
    quarantine_junit = tmp_path / "quarantine.junit.xml"
    primary_junit.write_text('<testsuite tests="1" failures="1"/>')
    quarantine_junit.write_text('<testsuite tests="1" errors="1"/>')
    report = {
        "format": "dish-pglite-development-report-v4",
        "certification_evidence": False,
        "native_postgresql_certified": False,
        "passed": False,
        "primary": {
            "collection_error": None,
            "nodes": [{
                "nodeid": "tests/postgresql/test_primary.py::test_case",
                "status": "failed",
                "assertion_failures": 1,
                "infrastructure_failures": 0,
                "detail": "expected row",
            }],
            "counts": {"assertion_failures": 1, "infrastructure_failures": 0},
            "aggregate_junit_path": str(primary_junit),
            "aggregate_junit_sha256": fr._sha256_file(primary_junit),
        },
        "quarantine": {
            "collection_error": None,
            "nodes": [{
                "nodeid": "tests/postgresql/test_quarantine.py::test_case",
                "status": "infrastructure",
                "assertion_failures": 0,
                "infrastructure_failures": 1,
                "detail": "connection refused",
            }],
            "counts": {"assertion_failures": 0, "infrastructure_failures": 1},
            "aggregate_junit_path": str(quarantine_junit),
            "aggregate_junit_sha256": fr._sha256_file(quarantine_junit),
        },
    }
    report_path = tmp_path / "pglite-report.json"
    report_path.write_text(json.dumps(report))

    failures = fr.collect_pglite_report_failures(
        output_dir=tmp_path, lane="pglite", source="dish-pg-pglite",
        report_path=report_path, command_exit=2,
    )

    assert {(item["source"], item["failure_kind"], item["invariant"]) for item in failures} == {
        ("dish-pg-pglite:primary", "assertion_failure", "tests/postgresql/test_primary.py::test_case"),
        ("dish-pg-pglite:quarantine", "infrastructure_failure", "tests/postgresql/test_quarantine.py::test_case"),
    }


    _state(tmp_path)
    _lanes(tmp_path, failed="pglite")
    evidence = fr.finalize_run(output_dir=tmp_path, evidence_path=tmp_path / "evidence.json")
    assert evidence["overall_result"] == "failed"
    assert set(evidence["lane_results"]["pglite"]["failure_ids"]) == {
        item["failure_id"] for item in failures
    }


def test_pglite_report_missing_junit_fails_evidence_closed(tmp_path: Path):
    report = {
        "format": "dish-pglite-development-report-v4",
        "certification_evidence": False,
        "native_postgresql_certified": False,
        "passed": True,
        "primary": {
            "collection_error": None, "nodes": [],
            "counts": {"assertion_failures": 0, "infrastructure_failures": 0},
            "aggregate_junit_path": str(tmp_path / "missing-primary.xml"),
            "aggregate_junit_sha256": "0" * 64,
        },
        "quarantine": {
            "collection_error": None, "nodes": [],
            "counts": {"assertion_failures": 0, "infrastructure_failures": 0},
            "aggregate_junit_path": str(tmp_path / "missing-quarantine.xml"),
            "aggregate_junit_sha256": "0" * 64,
        },
    }
    report_path = tmp_path / "pglite-report.json"
    report_path.write_text(json.dumps(report))

    failures = fr.collect_pglite_report_failures(
        output_dir=tmp_path, lane="pglite", source="dish-pg-pglite",
        report_path=report_path, command_exit=0,
    )

    assert len(failures) == 2
    assert {item["failure_kind"] for item in failures} == {"evidence_invalid"}
    assert all("aggregate JUnit evidence" in item["invariant"] for item in failures)


def test_evidence_schema_requires_all_governed_lanes_and_distinct_failure_contract():
    schema = json.loads(EVIDENCE_SCHEMA.read_text())
    assert set(schema["properties"]["lane_results"]["required"]) == set(fr.LANES)
    result = schema["$defs"]["result"]
    assert "failure_ids" in result["required"] and "failure_id" not in result["properties"]
    failure = schema["properties"]["failures"]["items"]
    assert {"source", "invariant"}.issubset(failure["required"])
