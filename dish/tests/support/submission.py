
"""Shared helpers extracted from test_dish_tool_step9_submit.py."""


import pytest

import sys

from pathlib import Path

from dish_tool.admin import DishAdminApplication
from tests.support.verification import make_app


def _signed(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    app, backend, operation_id, _ = make_app(tmp_path)
    review = app.execute("start", agent="codex", task_gid="t", kind="verification", run_id="submit-review", independence_attestation="independent")
    inspected = app.execute("inspect", agent="codex", submission_id=operation_id)
    assert inspected["ok"]
    approved = app.execute(
        "approve", agent="codex", model="gpt-5.6-sol", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id="submit-review",
    )
    assert approved["ok"]
    return app, backend, operation_id
