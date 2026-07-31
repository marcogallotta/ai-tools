
"""Shared helpers extracted from test_dish_tool_r42_authority_matrix.py."""


import dataclasses

import pytest

from dish_tool.admin import DishAdminApplication

from dish_tool.database import reserve_marco_authorizations

from dish_tool.errors import DishRuleError

from dish_tool.governed_diff import explicit_material_reasons, require_small_scope

from dish_tool.task_document import parse_task_document
from tests.support.readiness import _approve_and_submit
from tests.support.verification import TASK, make_app



def _review(app, *, run="review", agent="codex"):
    result = app.execute(
        "start", agent=agent, task_gid="t", kind="verification", run_id=run,
        independence_attestation="independent",
    )
    assert result["ok"]
    inspected = app.execute("inspect", agent=agent, submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result

def _authorize_dish_candidate(app, backend, operation_id, *, before="Test dish", after="Different dish"):
    admin = DishAdminApplication(
        app.conn, backend=backend,
        release_loader=lambda: app._load_release("verification"),
    )
    result = admin.execute(
        "authorize-governed-change", submission_id=operation_id,
        field="Dish candidate", before=before, after=after,
        reason="Marco authorized the candidate identity change", run_id="marco",
    )
    assert result["ok"]
    return result
