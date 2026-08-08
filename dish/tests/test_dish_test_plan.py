from __future__ import annotations

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


def test_integration_checkpoint_adds_full_suite() -> None:
    plan = build_plan(
        ["dish_tool/review_queue.py"],
        policy_path=POLICY,
        integration_checkpoint=True,
    )

    assert "ordinary full suite" in plan.lanes
    assert plan.commands[-1] == ".venv/bin/python -m pytest"


def test_agent_can_add_a_semantic_escalation_lane() -> None:
    plan = build_plan(
        ["dish_tool/review_queue.py"],
        policy_path=POLICY,
        add_lanes=["SQLite database-boundary"],
    )

    assert "SQLite database-boundary" in plan.lanes
    assert ".venv/bin/python -m pytest --database-boundary" in plan.commands



def test_reviewed_focused_test_advertises_parallel_safe_without_replacing_serial_by_default() -> None:
    plan = build_plan(["tests/test_commands.py"], policy_path=POLICY)

    assert plan.commands == (".venv/bin/python -m pytest -q tests/test_commands.py",)
    assert plan.parallel_safe_eligible is True
    assert plan.parallel_acceleration_used is False
    text = plan.to_text()
    assert "Parallel-safe focused execution is available" in text
    assert "--parallel-workers N" in text


def test_planner_can_select_supported_parallel_command_for_reviewed_focus() -> None:
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


def test_parallel_safe_focus_does_not_parallelize_governed_lanes() -> None:
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


def test_unchanged_reviewed_file_remains_parallel_eligible() -> None:
    from test_selection.parallel import parallel_safe_qualification_drift

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

    changed = tmp_path / "tests" / "test_commands.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("# changed after parallel review\n", encoding="utf-8")
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
