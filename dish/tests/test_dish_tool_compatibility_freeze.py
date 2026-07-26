import sqlite3

import pytest

from dish_tool.application_service import OperationApplicationService
from dish_tool.constants import SUPPORTED_PROTOCOL_VERSION
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.legacy_adapter import LegacyReadOnlyAdapter


def _legacy_row(conn: sqlite3.Connection, submission_id: str = "legacy") -> None:
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, required_verifier_family,
            status, created_at
        ) VALUES (?, 'task', 'initial', 'historical-test-release',
                  'commit', '{}', '{}', 'claude', 'claude', 'gpt',
                  'ready', 'now')
        """,
        (submission_id,),
    )


def test_legacy_records_remain_structurally_readable(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _legacy_row(conn)

    row = LegacyReadOnlyAdapter(conn).get("legacy")

    assert row is not None
    assert row["task_gid"] == "task"
    assert row["status"] == "ready"


def test_legacy_mutation_is_rejected_without_executing_old_workflow(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _legacy_row(conn)
    service = OperationApplicationService(conn)

    with pytest.raises(DishRuleError) as exc:
        service.route(
            "legacy", command="approve", protocol_version=SUPPORTED_PROTOCOL_VERSION
        )

    assert exc.value.code == "WRONG_STATE"
    assert exc.value.rule == "legacy_record_read_only"


def test_no_current_operation_is_fabricated_from_legacy_record(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _legacy_row(conn)
    service = OperationApplicationService(conn)

    routed = service.route(
        "legacy", command="inspect", protocol_version=SUPPORTED_PROTOCOL_VERSION
    )

    assert routed.generation == "legacy"
    assert routed.row["submission_id"] == "legacy"
    assert conn.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
