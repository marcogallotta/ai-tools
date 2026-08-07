
"""Shared helpers extracted from test_material_change_authority_matrix.py."""


import dataclasses

import pytest

from dish_tool.admin import DishAdminApplication

from dish_tool.database import reserve_marco_authorizations

from dish_tool.errors import DishRuleError

from dish_tool.governed_diff import explicit_material_reasons, require_small_scope

from dish_tool.task_document import parse_task_document
from tests.support.readiness import _approve_and_submit, _review
from tests.support.verification import TASK, make_app



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
