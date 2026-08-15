from __future__ import annotations

import json
import runpy
import shutil
import sys
from pathlib import Path

import pytest

from test_selection import execution_guard
from test_selection.execution_guard import require_safe_test_checkout

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = require_safe_test_checkout(ROOT)


def _lane_args(*args: str) -> list[str]:
    return [*args, "--expected-head", EXPECTED_HEAD]


def _namespace():
    return runpy.run_path(str(ROOT / "scripts" / "dish-test-lane"))


def test_named_lanes_are_complete_and_obvious() -> None:
    lanes = _namespace()["LANES"]
    assert tuple(sorted(lanes)) == (
        "command-api-contracts",
        "native-concurrency",
        "operational-certification",
        "parallel-safe",
        "pglite",
        "release-cutover",
        "round1c-journeys",
        "schema-migrations",
    )
    assert all(phases for phases in lanes.values())
    assert all(phase.name.strip() and phase.command for phases in lanes.values() for phase in phases)


def test_round1c_lane_names_the_observed_failure_journey_contracts() -> None:
    phase = _namespace()["LANES"]["round1c-journeys"][0]
    selected = set(phase.command[4:])
    expected = {
        "tests/test_safe_reclaim_workflow.py::test_resolved_execution_with_stranded_request_blocks_until_request_recovery",
        "tests/test_dish_partial_recovery_execution_journal.py::test_recover_inspect_settles_proven_requestless_execution_and_inspect_does_not_loop",
        "tests/test_safe_reclaim_workflow.py::test_same_expired_run_still_gets_recover_lease_not_safe_reclaim",
        "tests/test_abandonment_stage_successors.py::test_prepared_planning_claim_rejects_abandoned_run_then_binds_fresh_run",
        "tests/test_human_review_queue_workflow.py::test_service_review_queue_resolves_human_hold_by_current_row_number",
        "tests/test_semantic_proposal_bundle_workflow.py::test_governed_large_correction_queues_one_bundle_and_fresh_run_applies_it",
        "tests/test_semantic_proposal_bundle_workflow.py::test_approved_proposal_is_not_advertised_after_exact_content_staleness",
        "tests/test_action_surface_openapi.py::test_verification_action_schema_exposes_closed_correction_and_route_values",
        "tests/test_action_surface_openapi.py::test_inspect_openapi_requires_request_id_in_generated_and_checked_in_schema",
        "tests/test_authoritative_actions.py::test_submit_terminal_response_and_inspect_expose_no_stale_actions",
        "tests/test_change_start_intent.py::test_direct_create_change_requires_signed_baseline_before_insert",
        "tests/test_admin_round1b.py::test_inspect_resting_dish_by_frontend_url_uses_uuid_not_slug",
        "tests/test_admin_population_audit.py",
        "tests/test_admin_inspect_verbose.py",
        "tests/test_admin_attention.py::test_issues_treats_expired_open_lease_as_system_recoverable_not_marco_required",
        "tests/test_admin_attention.py::test_inspect_known_dish_remains_available_after_operator_moves_task_outside_cooking",
        "tests/test_admin_bulk_kill.py::test_bulk_kill_exact_precondition_does_not_kill_successor_run",
        "tests/test_human_review_choice_contract.py::test_review_queue_persists_ranked_choices_and_A_is_recommended",
        "tests/test_human_review_choice_contract.py::test_structured_choice_records_exact_unused_authorization_for_continuation",
        "tests/test_human_review_choice_contract.py::test_other_records_free_text_without_inventing_authorization",
    }
    assert expected <= selected


def test_lane_reuses_invoking_interpreter_instead_of_discovering_archive_venv() -> None:
    namespace = _namespace()
    assert namespace["PYTHON"] == sys.executable


def test_lane_stops_at_exact_failing_phase(monkeypatch) -> None:
    namespace = _namespace()
    calls: list[str] = []

    def fake_run(phase, *, env):
        calls.append(phase.name)
        return 7 if len(calls) == 2 else 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)
    assert main(_lane_args("schema-migrations")) == 7
    assert calls == [
        "focused schema and migration contracts",
        "SQLite database-boundary migration evidence",
    ]


def test_lane_rejects_stale_head_before_preflight_or_execution(monkeypatch) -> None:
    namespace = _namespace()
    main = namespace["main"]
    monkeypatch.setitem(
        main.__globals__,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("test phase must not start"),
    )
    monkeypatch.setitem(
        main.__globals__,
        "_xdist_preflight",
        lambda: pytest.fail("xdist preflight must not start"),
    )

    assert main(["parallel-safe", "--expected-head", "b" * 40, "--workers", "4"]) == 4


def test_lane_ignores_spoofed_primary_root_before_preflight_or_execution(monkeypatch) -> None:
    namespace = _namespace()
    main = namespace["main"]
    protected_primary = execution_guard._protected_primary_root()
    monkeypatch.setenv("DISH_PROTECTED_PRIMARY_ROOT", "/somewhere/else")
    monkeypatch.setattr(execution_guard, "_git", lambda _root, *args: {
        ("rev-parse", "--show-toplevel"): str(protected_primary),
        ("branch", "--show-current"): "",
        ("rev-parse", "HEAD"): EXPECTED_HEAD,
    }[args])
    monkeypatch.setitem(
        main.__globals__,
        "_run_phase",
        lambda *_args, **_kwargs: pytest.fail("test phase must not start"),
    )
    monkeypatch.setitem(
        main.__globals__,
        "_xdist_preflight",
        lambda: pytest.fail("xdist preflight must not start"),
    )

    assert main(_lane_args("parallel-safe", "--workers", "4")) == 4


def test_native_bootstrap_forwards_exact_candidate_head(monkeypatch) -> None:
    namespace = _namespace()
    captured = {}

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "status": "ready",
                "dsn": (
                    "postgresql+psycopg://dish_test:0ddca88b81a8bf1a15d84caa78efd7b3"
                    "@localhost:5432/dish_test"
                ),
            }
        )

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(namespace["subprocess"], "run", fake_run)
    namespace["_bootstrap_native_postgresql_env"]({"DISH_EXPECTED_HEAD": EXPECTED_HEAD})
    assert captured["command"][1:4] == [
        "scripts/dish-pg-native-certification",
        "--ensure-local-postgresql",
        "--expected-head",
    ]
    assert captured["command"][4] == EXPECTED_HEAD


def test_native_lane_reports_unavailable_before_running(monkeypatch, capsys) -> None:
    namespace = _namespace()
    phase = namespace["LANES"]["native-concurrency"][0]
    monkeypatch.setattr(namespace["subprocess"], "run", lambda *args, **kwargs: None)
    assert namespace["_run_phase"](phase, env={}) == 3
    assert "UNAVAILABLE [native PostgreSQL concurrency contracts]" in capsys.readouterr().err


def test_parallel_safe_can_run_serially_and_rejects_unreviewed_files(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)
    assert main(_lane_args("parallel-safe", "--test-file", "tests/test_commands.py")) == 0
    assert "-n" not in commands[-1]
    assert commands[-1][-1] == "tests/test_commands.py"

    main = _namespace()["main"]
    with pytest.raises(SystemExit):
        main(
            [
                "parallel-safe",
                "--test-file",
                "tests/test_lease_authority.py",
            ]
        )


def test_parallel_safe_workers_use_invoking_environment_and_exact_selection(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_xdist_preflight", lambda: 0)
    monkeypatch.setitem(
        main.__globals__,
        "require_parallel_safe_qualification",
        lambda selected: tuple(selected),
    )
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)

    assert main(
        _lane_args(
            "parallel-safe",
            "--workers",
            "4",
            "--test-file",
            "tests/test_commands.py",
        )
    ) == 0
    command = commands[-1]
    assert command[:10] == (
        namespace["PYTHON"],
        "-m",
        "pytest",
        "-p",
        "no:randomly",
        "-n",
        "4",
        "--dist",
        "loadfile",
        "-q",
    )
    assert command[10:] == ("tests/test_commands.py",)


def test_parallel_safe_reports_xdist_missing_from_primary_environment(monkeypatch, capsys) -> None:
    namespace = _namespace()
    class _Completed:
        returncode = 1

    monkeypatch.setattr(namespace["subprocess"], "run", lambda *args, **kwargs: _Completed())
    assert namespace["_xdist_preflight"]() == 3
    assert "install requirements-test.txt" in capsys.readouterr().err


def test_diagnostic_mode_changes_output_only_not_pytest_selection(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)
    expected_files = tuple(namespace["LANES"]["release-cutover"][0].command[4:])

    assert main(_lane_args("release-cutover", "--diagnose")) == 0
    command = commands[-1]
    assert "-q" not in command
    assert command[-2:] == ("-vv", "--durations=20")
    assert tuple(part for part in command if part.endswith(".py")) == expected_files


def _parallel_fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "test_selection").mkdir(parents=True)
    shutil.copy2(
        ROOT / "test_selection" / "parallel_safe_qualifications.json",
        tmp_path / "test_selection" / "parallel_safe_qualifications.json",
    )
    (tmp_path / "tests").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "tests" / "conftest.py", tmp_path / "tests" / "conftest.py")
    shutil.copytree(ROOT / "tests" / "support", tmp_path / "tests" / "support")
    shutil.copy2(ROOT / "tests" / "test_commands.py", tmp_path / "tests" / "test_commands.py")
    return tmp_path


def _exercise_files(target: str = "tests/test_commands.py") -> list[str]:
    return [target, *[f"tests/witness_{index}.py" for index in range(7)]]


def _file_results(exercise_files: list[str]) -> dict[str, dict[str, int]]:
    return {
        path: {"tests": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
        for path in exercise_files
    }


def _qualification_run(
    exercise_files: list[str], *, workers: int | None, repeat: int
) -> dict[str, object]:
    return {
        "workers": workers,
        "repeat": repeat,
        "returncode": 0,
        "wall_seconds": 0.01,
        "tests": len(exercise_files),
        "passed": len(exercise_files),
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "file_results": _file_results(exercise_files),
    }


def _qualification_environment() -> dict[str, object]:
    return {
        "python_executable": "/repo/.venv/bin/python",
        "python_version": "3.13.5",
        "platform": "test-platform",
        "cpu_count": 8,
        "pytest_version": "9.1.1",
        "xdist_version": "3.8.0",
        "execnet_version": "2.1.2",
        "requirements_test_sha256": "r" * 64,
        "git_head": "test-head",
        "git_status": "",
    }


def test_parallel_safe_drift_blocks_explicit_workers_but_keeps_serial_diagnosis_usable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import test_selection.parallel as parallel

    isolated = _parallel_fixture_root(tmp_path)
    changed = isolated / "tests" / "test_commands.py"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# changed after review\n", encoding="utf-8")
    monkeypatch.setattr(parallel, "ROOT", isolated)

    namespace = _namespace()
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_xdist_preflight",
        lambda: pytest.fail("xdist preflight must not run after qualification drift"),
    )
    with pytest.raises(SystemExit) as excinfo:
        namespace["main"](
            [
                "parallel-safe",
                "--workers",
                "4",
                "--test-file",
                "tests/test_commands.py",
            ]
        )
    assert excinfo.value.code == 2
    error = capsys.readouterr().err
    assert "parallel-safe qualification drift" in error
    assert "changed since parallel review" in error
    assert "dish-parallel-safe-qualify" in error

    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    namespace = _namespace()
    monkeypatch.setitem(namespace["main"].__globals__, "_run_phase", fake_run)
    assert namespace["main"](
        _lane_args("parallel-safe", "--test-file", "tests/test_commands.py")
    ) == 0
    assert "-n" not in commands[-1]
    assert commands[-1][-1] == "tests/test_commands.py"


def test_full_parallel_safe_inventory_partitions_drift_to_serial_fallback(monkeypatch, capsys) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    monkeypatch.setitem(
        namespace["main"].__globals__,
        "parallel_safe_partition",
        lambda _selected: (
            ("tests/test_commands.py",),
            (("tests/test_pagination.py", "changed since parallel review"),),
        ),
    )
    monkeypatch.setitem(namespace["main"].__globals__, "_xdist_preflight", lambda: 0)
    monkeypatch.setitem(namespace["main"].__globals__, "_run_phase", fake_run)

    assert namespace["main"](_lane_args("parallel-safe", "--workers", "4")) == 0
    assert len(commands) == 2
    assert ("-n", "4", "--dist", "loadfile") == commands[0][5:9]
    assert commands[0][-1] == "tests/test_commands.py"
    assert "-n" not in commands[1]
    assert commands[1][-1] == "tests/test_pagination.py"
    assert "QUALIFICATION FALLBACK" in capsys.readouterr().err


def test_shared_fixture_drift_invalidates_parallel_qualification(tmp_path: Path) -> None:
    import test_selection.parallel as parallel

    isolated = _parallel_fixture_root(tmp_path)
    conftest = isolated / "tests" / "conftest.py"
    conftest.write_text(conftest.read_text(encoding="utf-8") + "\n# shared change\n", encoding="utf-8")

    reason = parallel.parallel_safe_qualification_reason(
        "tests/test_commands.py", root=isolated
    )
    assert reason == (
        "shared fixtures/helpers changed since parallel review; requires explicit requalification"
    )


def test_hash_only_manifest_edit_cannot_bypass_evidence_requirement(tmp_path: Path) -> None:
    import test_selection.parallel as parallel

    isolated = _parallel_fixture_root(tmp_path)
    changed = isolated / "tests" / "test_commands.py"
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    manifest_path = isolated / "test_selection" / "parallel_safe_qualifications.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["tests/test_commands.py"]["reviewed_sha256"] = parallel._hash_file(changed)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    reason = parallel.parallel_safe_qualification_reason(
        "tests/test_commands.py", root=isolated
    )
    assert reason == "qualification evidence does not match the reviewed file identity"


def test_active_batch_evidence_tampering_fails_closed(tmp_path: Path) -> None:
    import test_selection.parallel as parallel

    isolated = _parallel_fixture_root(tmp_path)
    shutil.copy2(ROOT / "tests" / "test_batch_apply.py", isolated / "tests" / "test_batch_apply.py")
    manifest_path = isolated / "test_selection" / "parallel_safe_qualifications.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    active = manifest["files"]["tests/test_batch_apply.py"]["active_evidence"]
    assert active["kind"] == "batch"
    manifest["batch_evidence"][active["id"]]["note"] += " tampered"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert parallel.parallel_safe_qualification_reason(
        "tests/test_batch_apply.py", root=isolated
    ) == "qualification evidence content does not match its evidence ID"


def test_active_per_file_evidence_tampering_fails_closed(tmp_path: Path) -> None:
    import test_selection.parallel as parallel

    isolated = _parallel_fixture_root(tmp_path)
    target = "tests/test_dish_tool_step8_routes.py"
    shutil.copy2(ROOT / target, isolated / target)
    manifest_path = isolated / "test_selection" / "parallel_safe_qualifications.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    active = manifest["files"][target]["active_evidence"]
    assert active["kind"] == "file"
    history = manifest["files"][target]["history"]
    evidence = next(item for item in history if item.get("evidence_id") == active["id"])
    evidence["note"] += " tampered"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert parallel.parallel_safe_qualification_reason(
        target, root=isolated
    ) == "qualification evidence content does not match its evidence ID"


def test_requalification_tool_records_only_after_required_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-parallel-safe-qualify"))
    calls: list[tuple[int | None, int]] = []
    recorded: dict[str, object] = {}

    monkeypatch.setitem(namespace["main"].__globals__, "PENDING_ROOT", tmp_path / "pending")
    monkeypatch.setitem(namespace["main"].__globals__, "_static_findings", lambda _path: [])
    monkeypatch.setitem(namespace["main"].__globals__, "_hash_file", lambda _path: "f" * 64)
    monkeypatch.setitem(
        namespace["main"].__globals__, "parallel_safe_shared_sha256", lambda _root: "s" * 64
    )
    environment = _qualification_environment()
    monkeypatch.setitem(
        namespace["main"].__globals__, "_environment_metadata", lambda _path: environment
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "parallel_safe_witness_files",
        lambda _path, *, root: tuple(f"tests/witness_{index}.py" for index in range(7)),
    )
    monkeypatch.setitem(namespace["main"].__globals__, "_verify_xdist", lambda: None)

    def fake_run_pytest(*, exercise_files, workers, repeat, artifact_dir):
        assert exercise_files[0] == "tests/test_commands.py"
        assert len(exercise_files) == 8
        calls.append((workers, repeat))
        return _qualification_run(exercise_files, workers=workers, repeat=repeat)

    def fake_record(path, evidence, *, root):
        recorded["path"] = path
        recorded["evidence"] = evidence
        return "evidence-id"

    monkeypatch.setitem(namespace["main"].__globals__, "_run_pytest", fake_run_pytest)
    monkeypatch.setitem(
        namespace["main"].__globals__, "record_parallel_safe_qualification", fake_record
    )

    common = ["--test-file", "tests/test_commands.py", "--reviewer", "test-reviewer"]
    assert namespace["main"]([*common, "--phase", "serial"]) == 0
    assert recorded == {}
    assert namespace["main"]([*common, "--phase", "2"]) == 0
    assert namespace["main"]([*common, "--phase", "4"]) == 0
    assert namespace["main"]([*common, "--phase", "8"]) == 0
    assert recorded == {}
    assert namespace["main"]([*common, "--phase", "finalize"]) == 0

    assert calls == [
        (None, 1),
        (2, 1), (2, 2), (2, 3),
        (4, 1), (4, 2), (4, 3),
        (8, 1), (8, 2), (8, 3),
    ]
    evidence = recorded["evidence"]
    assert evidence["reviewer"] == "test-reviewer"
    assert evidence["static_review"]["findings"] == []
    assert evidence["environment"] == environment
    assert evidence["environment_identity"]
    assert len(evidence["parallel"]["2"]) == 3
    assert len(evidence["parallel"]["4"]) == 3
    assert len(evidence["parallel"]["8"]) == 3


def test_requalification_tool_does_not_run_when_static_scan_flags_risk(
    tmp_path: Path, monkeypatch
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-parallel-safe-qualify"))
    monkeypatch.setitem(namespace["main"].__globals__, "PENDING_ROOT", tmp_path / "pending")
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_static_findings",
        lambda _path: [
            {"file": "tests/test_commands.py", "line": 1, "kind": "environment", "call": "setenv"}
        ],
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_run_pytest",
        lambda **_kwargs: pytest.fail("pytest must not run after static isolation findings"),
    )
    assert namespace["main"](
        [
            "--test-file",
            "tests/test_commands.py",
            "--reviewer",
            "test-reviewer",
            "--phase",
            "serial",
        ]
    ) == 2


def test_requalification_finalize_refuses_incomplete_worker_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-parallel-safe-qualify"))
    monkeypatch.setitem(namespace["main"].__globals__, "PENDING_ROOT", tmp_path / "pending")
    monkeypatch.setitem(namespace["main"].__globals__, "_static_findings", lambda _path: [])
    monkeypatch.setitem(namespace["main"].__globals__, "_hash_file", lambda _path: "f" * 64)
    monkeypatch.setitem(
        namespace["main"].__globals__, "parallel_safe_shared_sha256", lambda _root: "s" * 64
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_environment_metadata",
        lambda _path: _qualification_environment(),
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "parallel_safe_witness_files",
        lambda _path, *, root: tuple(f"tests/witness_{index}.py" for index in range(7)),
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_run_pytest",
        lambda **kwargs: _qualification_run(
            kwargs["exercise_files"], workers=kwargs["workers"], repeat=kwargs["repeat"]
        ),
    )
    common = ["--test-file", "tests/test_commands.py", "--reviewer", "test-reviewer"]
    assert namespace["main"]([*common, "--phase", "serial"]) == 0
    assert namespace["main"]([*common, "--phase", "finalize"]) == 2


def test_requalification_phases_are_bound_to_serial_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-parallel-safe-qualify"))
    monkeypatch.setitem(namespace["main"].__globals__, "PENDING_ROOT", tmp_path / "pending")
    monkeypatch.setitem(namespace["main"].__globals__, "_static_findings", lambda _path: [])
    monkeypatch.setitem(namespace["main"].__globals__, "_hash_file", lambda _path: "f" * 64)
    monkeypatch.setitem(
        namespace["main"].__globals__, "parallel_safe_shared_sha256", lambda _root: "s" * 64
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "parallel_safe_witness_files",
        lambda _path, *, root: tuple(f"tests/witness_{index}.py" for index in range(7)),
    )
    state = {"environment": _qualification_environment()}
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_environment_metadata",
        lambda _path: state["environment"],
    )
    calls: list[tuple[int | None, int]] = []

    def fake_run_pytest(*, exercise_files, workers, repeat, artifact_dir):
        calls.append((workers, repeat))
        return _qualification_run(exercise_files, workers=workers, repeat=repeat)

    monkeypatch.setitem(namespace["main"].__globals__, "_run_pytest", fake_run_pytest)
    common = ["--test-file", "tests/test_commands.py", "--reviewer", "test-reviewer"]
    assert namespace["main"]([*common, "--phase", "serial"]) == 0

    changed = dict(state["environment"])
    changed["pytest_version"] = "9.9.9"
    state["environment"] = changed
    assert namespace["main"]([*common, "--phase", "2"]) == 2
    assert namespace["main"]([*common, "--phase", "finalize"]) == 2
    assert calls == [(None, 1)]
    error = capsys.readouterr().err
    assert "qualification environment changed since --phase serial" in error
    assert "pytest_version='9.1.1'->'9.9.9'" in error
    assert "restart from --phase serial" in error


def test_requalification_serial_rejects_zero_test_target_participation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-parallel-safe-qualify"))
    pending_root = tmp_path / "pending"
    monkeypatch.setitem(namespace["main"].__globals__, "PENDING_ROOT", pending_root)
    monkeypatch.setitem(namespace["main"].__globals__, "_static_findings", lambda _path: [])
    monkeypatch.setitem(namespace["main"].__globals__, "_hash_file", lambda _path: "f" * 64)
    monkeypatch.setitem(
        namespace["main"].__globals__, "parallel_safe_shared_sha256", lambda _root: "s" * 64
    )
    monkeypatch.setitem(
        namespace["main"].__globals__, "_environment_metadata", lambda _path: _qualification_environment()
    )
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "parallel_safe_witness_files",
        lambda _path, *, root: tuple(f"tests/witness_{index}.py" for index in range(7)),
    )

    def zero_target(*, exercise_files, workers, repeat, artifact_dir):
        result = _qualification_run(exercise_files, workers=workers, repeat=repeat)
        result["file_results"][exercise_files[0]] = {
            "tests": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        return result

    monkeypatch.setitem(namespace["main"].__globals__, "_run_pytest", zero_target)
    assert namespace["main"](
        [
            "--test-file",
            "tests/test_commands.py",
            "--reviewer",
            "test-reviewer",
            "--phase",
            "serial",
        ]
    ) == 2
    assert not pending_root.exists()
    assert "does not prove executed passing tests for tests/test_commands.py" in capsys.readouterr().err


def test_evidence_validator_requires_witness_participation_and_worker_repeat_metadata() -> None:
    import test_selection.parallel as parallel

    exercise_files = _exercise_files()
    environment = _qualification_environment()
    file_hash = "f" * 64
    shared_hash = "s" * 64
    evidence = {
        "kind": "file",
        "files": [exercise_files[0]],
        "file_sha256": {exercise_files[0]: file_hash},
        "exercise_files": exercise_files,
        "witness_sha256": {path: "w" * 64 for path in exercise_files[1:]},
        "shared_sha256": shared_hash,
        "static_review": {"findings": []},
        "serial": [_qualification_run(exercise_files, workers=None, repeat=1)],
        "parallel": {
            str(workers): [
                _qualification_run(exercise_files, workers=workers, repeat=repeat)
                for repeat in range(1, 4)
            ]
            for workers in (2, 4, 8)
        },
        "environment": environment,
        "environment_identity": parallel.qualification_environment_id(environment),
    }

    missing_witness = json.loads(json.dumps(evidence))
    missing_witness["parallel"]["4"][0]["file_results"].pop(exercise_files[-1])
    missing_witness["evidence_id"] = parallel.qualification_evidence_id(missing_witness)
    assert parallel._validate_evidence(
        missing_witness,
        path=exercise_files[0],
        reviewed_sha256=file_hash,
        shared_sha256=shared_hash,
    ) == f"qualification -n 4 evidence is invalid: qualification run does not prove participation for {exercise_files[-1]}"

    wrong_workers = json.loads(json.dumps(evidence))
    wrong_workers["parallel"]["8"][0]["workers"] = 4
    wrong_workers["evidence_id"] = parallel.qualification_evidence_id(wrong_workers)
    assert parallel._validate_evidence(
        wrong_workers,
        path=exercise_files[0],
        reviewed_sha256=file_hash,
        shared_sha256=shared_hash,
    ) == "qualification -n 8 evidence is invalid: qualification run records workers=4; expected 8"

    duplicate_repeat = json.loads(json.dumps(evidence))
    duplicate_repeat["parallel"]["2"][2]["repeat"] = 2
    duplicate_repeat["evidence_id"] = parallel.qualification_evidence_id(duplicate_repeat)
    assert parallel._validate_evidence(
        duplicate_repeat,
        path=exercise_files[0],
        reviewed_sha256=file_hash,
        shared_sha256=shared_hash,
    ) == "qualification evidence requires distinct -n 2 repetitions 1..3"


def test_recorded_requalification_updates_only_selected_file_block(tmp_path: Path) -> None:
    import test_selection.parallel as parallel

    isolated = _parallel_fixture_root(tmp_path)
    manifest_path = isolated / "test_selection" / "parallel_safe_qualifications.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    other_before = before["files"]["tests/test_pagination.py"]
    file_hash = parallel._hash_file(isolated / "tests" / "test_commands.py")
    shared_hash = parallel.parallel_safe_shared_sha256(isolated)

    exercise_files = _exercise_files()
    environment = _qualification_environment()
    success = _qualification_run(exercise_files, workers=None, repeat=1)
    evidence = {
        "kind": "file",
        "qualified_at": "2026-08-08T00:00:00+00:00",
        "started_at": "2026-08-08T00:00:00+00:00",
        "reviewer": "agent-a",
        "note": "unit test",
        "files": ["tests/test_commands.py"],
        "file_sha256": {"tests/test_commands.py": file_hash},
        "exercise_files": exercise_files,
        "witness_sha256": {f"tests/witness_{index}.py": "w" * 64 for index in range(7)},
        "shared_sha256": shared_hash,
        "shared_scope": list(parallel.SHARED_QUALIFICATION_INPUTS),
        "static_review": {"tool": "tests.flake_runner.scan_flake_risks", "findings": []},
        "serial": [success],
        "parallel": {
            str(workers): [
                _qualification_run(exercise_files, workers=workers, repeat=repeat)
                for repeat in range(1, 4)
            ]
            for workers in (2, 4, 8)
        },
        "pytest_args": ["-p", "no:randomly", "--dist", "loadfile"],
        "environment": environment,
        "environment_identity": parallel.qualification_environment_id(environment),
    }

    evidence_id = parallel.record_parallel_safe_qualification(
        "tests/test_commands.py", evidence, root=isolated
    )
    after = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert after["files"]["tests/test_pagination.py"] == other_before
    selected = after["files"]["tests/test_commands.py"]
    assert selected["active_evidence"] == {"kind": "file", "id": evidence_id}
    assert selected["history"][-1]["reviewer"] == "agent-a"
    assert selected["history"][-1]["evidence_id"] == evidence_id
