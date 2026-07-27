import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

from dish_tool.database import (
    record_marco_authorization,
    reserve_marco_authorizations,
    consume_reserved_marco_authorizations,
)
from dish_tool.errors import DishRuleError
from test_dish_tool_step6_prepare import TASK, Backend, app, write
from test_dish_tool_step7_verification import make_app


def test_marco_authorizations_reserve_all_or_nothing(tmp_path):
    application, _, operation_id, _ = make_app(tmp_path)
    first = record_marco_authorization(
        application.conn, task_gid="t", operation_id=operation_id,
        field_name="Purpose", before="old", after="new", reason="approved",
    )
    changes = (
        {"field": "Purpose", "before": "old", "after": "new"},
        {"field": "Locks", "before": "keep", "after": "remove"},
    )
    with pytest.raises(DishRuleError) as exc:
        reserve_marco_authorizations(
            application.conn, task_gid="t", operation_id=operation_id, changes=changes
        )
    assert exc.value.rule == "governed_change_unauthorized"
    row = application.conn.execute(
        "SELECT reserved_by_operation_id, consumed_at FROM marco_authorizations WHERE authorization_id=?",
        (first["authorization_id"],),
    ).fetchone()
    assert tuple(row) == (None, None)

    second = record_marco_authorization(
        application.conn, task_gid="t", operation_id=operation_id,
        field_name="Locks", before="keep", after="remove", reason="approved",
    )
    reserved = reserve_marco_authorizations(
        application.conn, task_gid="t", operation_id=operation_id, changes=changes
    )
    ids = tuple(row["authorization_id"] for row in reserved)
    assert set(ids) == {first["authorization_id"], second["authorization_id"]}
    consume_reserved_marco_authorizations(
        application.conn, operation_id=operation_id,
        authorization_ids=ids, candidate_identity="sha256:candidate",
    )
    consumed = application.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE consumed_identity='sha256:candidate'"
    ).fetchone()[0]
    assert consumed == 2


def test_initial_prepare_owns_researched_by(tmp_path):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="codex", task_gid="t", kind="initial",
        change_level=None, change_reason=None, run_id="constructor-run",
    )
    hostile = TASK.replace(
        "Researched by: ChatGPT — GPT-5, 2026-07-25", "Researched by: None"
    )
    result = application.execute(
        "prepare", model="gpt-5.6-sol", agent="codex", submission_id=started["submission_id"],
        file_path=write(tmp_path, "hostile.txt", hostile),
    )
    assert result["ok"]
    assert "Researched by: None" not in backend.notes
    assert "Researched by: Codex — gpt-5.6-sol" in backend.notes
