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
            "dish_pg/migrations/versions/0028_consumed_first_request_open_admission.py",
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
