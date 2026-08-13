from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_selection.model import PolicyError, load_policy
from test_selection.planner import build_plan


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "test_selection" / "ownership.csv"


def test_ordinary_authority_change_selects_focused_owners_and_smoke() -> None:
    plan = build_plan(["dish_tool/review_queue.py"], policy_path=POLICY)

    assert "tests/test_human_review_queue_workflow.py" in plan.focused_tests
    assert "tests/test_admin_review_queue_human_output.py" in plan.focused_tests
    assert "smoke" in plan.lanes
    assert "ordinary full suite" not in plan.lanes
    assert plan.commands[0].startswith(".venv/bin/python -m pytest -q ")


def test_mixed_migration_and_release_change_takes_lane_union() -> None:
    plan = build_plan(
        [
            "dish_pg/migrations/versions/0029_cutover_authority_admission_fixes.py",
            "dish_pg/release_validation.py",
        ],
        policy_path=POLICY,
    )

    assert {
        "PGlite primary",
        "SQLite database-boundary",
        "native PostgreSQL certification",
        "smoke",
        "Stage A mutation sample",
        "source acceptance",
    }.issubset(set(plan.lanes))
    assert "ordinary full suite" not in plan.lanes
    assert len([command for command in plan.commands if "dish-pg-pglite" in command]) == 1


def test_policy_data_change_does_not_force_full_suite_by_itself() -> None:
    plan = build_plan(["test_selection/ownership.csv"], policy_path=POLICY)

    assert "ordinary full suite" not in plan.lanes
    assert "tests/test_dish_test_map_validation.py" in plan.focused_tests
    assert plan.conditional_reviews


def test_integration_checkpoint_does_not_broaden_selector_required_lanes() -> None:
    plan = build_plan(
        ["dish_tool/review_queue.py"],
        policy_path=POLICY,
        integration_checkpoint=True,
    )

    assert plan.integration_checkpoint is True
    assert "ordinary full suite" not in plan.lanes
    assert ".venv/bin/python -m pytest" not in plan.commands


def test_ordinary_python_selection_stays_focused() -> None:
    plan = build_plan(["dish_service/config.py"], policy_path=POLICY)

    assert {"focused ordinary", "smoke"}.issubset(set(plan.lanes))
    assert "frontend static/tooling" not in plan.lanes
    assert "browser acceptance" not in plan.lanes
    assert "native PostgreSQL certification" not in plan.lanes
    assert "ordinary full suite" not in plan.lanes


def test_high_consequence_selector_control_change_still_fails_closed_to_full_suite() -> None:
    plan = build_plan(["test_selection/planner.py"], policy_path=POLICY)

    assert "ordinary full suite" in plan.lanes
    assert plan.commands[-1] == ".venv/bin/python -m pytest"


def test_frontend_static_tooling_is_independently_selectable() -> None:
    plan = build_plan(["frontend/tools/lint.mjs"], policy_path=POLICY)

    assert "frontend static/tooling" in plan.lanes
    assert "browser acceptance" not in plan.lanes
    assert "npm --prefix frontend run check:static" in plan.commands
    assert "npm --prefix frontend run test:acceptance" not in plan.commands


def test_browser_relevant_service_contract_selects_browser_without_static_lane() -> None:
    plan = build_plan(["dish_service/frontend_http.py"], policy_path=POLICY)

    assert "browser acceptance" in plan.lanes
    assert "frontend static/tooling" not in plan.lanes
    assert "npm --prefix frontend run test:acceptance" in plan.commands


def test_frontend_runtime_source_selects_static_and_browser_boundaries() -> None:
    plan = build_plan(["frontend/src/js/features/auth/session.js"], policy_path=POLICY)

    assert {"frontend static/tooling", "browser acceptance"}.issubset(set(plan.lanes))
    assert "npm --prefix frontend run check:static" in plan.commands
    assert "npm --prefix frontend run test:acceptance" in plan.commands


def test_native_postgresql_runtime_mapping_is_not_only_advisory() -> None:
    plan = build_plan(["dish_pg/repositories.py"], policy_path=POLICY)

    assert "native PostgreSQL certification" in plan.lanes
    assert any("dish-pg-native-certification" in command for command in plan.commands)
    assert "ordinary full suite" not in plan.lanes


def test_agent_can_add_a_semantic_escalation_lane() -> None:
    plan = build_plan(
        ["dish_tool/review_queue.py"],
        policy_path=POLICY,
        add_lanes=["SQLite database-boundary"],
    )

    assert "SQLite database-boundary" in plan.lanes
    assert ".venv/bin/python -m pytest --database-boundary" in plan.commands



def _write_synthetic_qualified_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """Install a self-contained, currently-qualified evidence set for an inventoried file.

    The real ``tests/test_commands.py`` batch (test_selection/parallel_safe_qualifications.json)
    is legitimately drifted right now because tests/test_core_models_and_dispatch.py, a member of
    that same reviewed batch, changed during cleanup iteration 1. This helper builds an isolated,
    freshly-qualified manifest under tmp_path instead of relying on that real (drifted) evidence,
    so positive-path planner behavior stays covered without re-qualifying the real batch.
    """
    import test_selection.parallel as parallel

    (tmp_path / "tests").mkdir()
    shutil.copy2(ROOT / target, tmp_path / target)
    shutil.copy2(ROOT / "tests" / "conftest.py", tmp_path / "tests" / "conftest.py")
    shutil.copytree(ROOT / "tests" / "support", tmp_path / "tests" / "support")
    monkeypatch.setattr(parallel, "ROOT", tmp_path)

    reviewed_sha256 = parallel._hash_file(tmp_path / target)
    shared_sha256 = parallel.parallel_safe_shared_sha256(tmp_path)
    successful_run = {"returncode": 0, "failed": 0, "errors": 0, "passed": 1}
    evidence_payload = {
        "kind": "batch",
        "files": [target],
        "file_sha256": {target: reviewed_sha256},
        "shared_sha256": shared_sha256,
        "static_review": {"findings": []},
        "serial": [{**successful_run, "workers": None, "repeat": 1}],
        "parallel": {
            str(workers): [
                {**successful_run, "workers": workers, "repeat": repeat} for repeat in (1, 2, 3)
            ]
            for workers in parallel.REQUIRED_WORKER_COUNTS
        },
    }
    evidence_id = parallel.qualification_evidence_id(evidence_payload)
    evidence_payload["evidence_id"] = evidence_id

    manifest = {
        "schema_version": parallel.QUALIFICATION_SCHEMA_VERSION,
        "inventory": [target],
        "files": {
            target: {
                "reviewed_sha256": reviewed_sha256,
                "shared_sha256": shared_sha256,
                "active_evidence": {"kind": "batch", "id": evidence_id},
            }
        },
        "batch_evidence": {evidence_id: evidence_payload},
    }
    (tmp_path / "test_selection").mkdir(parents=True)
    (tmp_path / "test_selection" / "parallel_safe_qualifications.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_reviewed_focused_test_advertises_parallel_safe_without_replacing_serial_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_synthetic_qualified_batch(tmp_path, monkeypatch, "tests/test_commands.py")

    plan = build_plan(["tests/test_commands.py"], policy_path=POLICY)

    assert plan.commands == (".venv/bin/python -m pytest -q tests/test_commands.py",)
    assert plan.parallel_safe_eligible is True
    assert plan.parallel_acceleration_used is False
    text = plan.to_text()
    assert "Parallel-safe focused execution is available" in text
    assert "--parallel-workers N" in text


def test_planner_can_select_supported_parallel_command_for_reviewed_focus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_synthetic_qualified_batch(tmp_path, monkeypatch, "tests/test_commands.py")

    plan = build_plan(
        ["tests/test_commands.py"],
        policy_path=POLICY,
        parallel_workers=4,
    )

    assert plan.commands == (
        ".venv/bin/python scripts/dish-test-lane parallel-safe --workers 4 "
        "--test-file tests/test_commands.py",
    )
    assert plan.parallel_acceleration_used is True
    assert "Governed serial lanes, when present, remain serial" in plan.to_text()


def test_parallel_safe_focus_does_not_parallelize_governed_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_synthetic_qualified_batch(tmp_path, monkeypatch, "tests/test_commands.py")

    plan = build_plan(
        ["tests/test_commands.py"],
        policy_path=POLICY,
        add_lanes=["SQLite database-boundary"],
        parallel_workers=4,
    )

    assert plan.commands == (
        ".venv/bin/python scripts/dish-test-lane parallel-safe --workers 4 "
        "--test-file tests/test_commands.py",
        ".venv/bin/python -m pytest --database-boundary",
    )


def test_planner_keeps_unreviewed_focused_test_serial_when_parallel_is_requested() -> None:
    plan = build_plan(
        ["tests/test_lease_authority.py"],
        policy_path=POLICY,
        parallel_workers=2,
    )

    assert plan.parallel_safe_eligible is False
    assert plan.parallel_acceleration_used is False
    assert plan.commands == (".venv/bin/python -m pytest -q tests/test_lease_authority.py",)
    assert plan.parallel_blockers == ("tests/test_lease_authority.py",)
    assert "focused command remains serial" in plan.to_text()


def test_deleted_path_can_use_base_revision_policy(tmp_path: Path) -> None:
    current_rows = POLICY.read_text(encoding="utf-8").splitlines()
    header, *rows = current_rows
    current_without_path = tmp_path / "current.csv"
    current_without_path.write_text(
        "\n".join(
            [header, *[row for row in rows if not row.startswith("dish_tool/review_queue.py,")]]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = build_plan(
        ["dish_tool/review_queue.py"],
        policy_path=current_without_path,
        fallback_policy=load_policy(POLICY),
    )

    assert "tests/test_human_review_queue_workflow.py" in plan.focused_tests
    assert "smoke" in plan.lanes


def test_unclassified_path_fails_closed() -> None:
    with pytest.raises(PolicyError, match="unclassified changed paths"):
        build_plan(["dish_tool/future_unclassified_module.py"], policy_path=POLICY)


class _CollectionConfig:
    def __init__(self, root: Path, args: list[str]) -> None:
        self.rootpath = root
        self.args = args


def test_complete_collection_detection_distinguishes_focused_paths() -> None:
    from tests.conftest import _is_complete_repository_collection

    assert _is_complete_repository_collection(_CollectionConfig(ROOT, [str(ROOT)]))
    assert _is_complete_repository_collection(_CollectionConfig(ROOT, [str(ROOT / "tests")]))
    assert not _is_complete_repository_collection(
        _CollectionConfig(ROOT, ["tests/test_dish_test_plan.py"])
    )


def test_unchanged_reviewed_file_remains_parallel_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_selection.parallel import parallel_safe_qualification_drift

    _write_synthetic_qualified_batch(tmp_path, monkeypatch, "tests/test_commands.py")

    assert parallel_safe_qualification_drift(["tests/test_commands.py"]) == ()
    plan = build_plan(
        ["tests/test_commands.py"],
        policy_path=POLICY,
        parallel_workers=4,
    )
    assert plan.parallel_safe_eligible is True
    assert plan.parallel_acceleration_used is True


def test_changed_reviewed_file_keeps_planner_serial_and_reports_qualification_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import test_selection.parallel as parallel

    (tmp_path / "test_selection").mkdir(parents=True)
    shutil.copy2(
        ROOT / "test_selection" / "parallel_safe_qualifications.json",
        tmp_path / "test_selection" / "parallel_safe_qualifications.json",
    )
    (tmp_path / "tests").mkdir()
    shutil.copy2(ROOT / "tests" / "conftest.py", tmp_path / "tests" / "conftest.py")
    shutil.copytree(ROOT / "tests" / "support", tmp_path / "tests" / "support")
    changed = tmp_path / "tests" / "test_commands.py"
    shutil.copy2(ROOT / "tests" / "test_commands.py", changed)
    changed.write_text(changed.read_text(encoding="utf-8") + "\n# changed after review\n", encoding="utf-8")
    monkeypatch.setattr(parallel, "ROOT", tmp_path)

    plan = build_plan(
        ["tests/test_commands.py"],
        policy_path=POLICY,
        parallel_workers=4,
    )

    assert plan.parallel_safe_eligible is False
    assert plan.parallel_acceleration_used is False
    assert plan.commands == (".venv/bin/python -m pytest -q tests/test_commands.py",)
    assert plan.parallel_blockers == (
        "tests/test_commands.py (changed since parallel review; requires explicit requalification)",
    )
    assert "changed since parallel review" in plan.to_text()
