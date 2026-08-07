
"""Shared helpers extracted from test_recovery_readiness.py."""


from pathlib import Path

import sqlite3

import pytest

from dish_tool import step6, step8

from dish_tool.admin import DishAdminApplication

from dish_tool.errors import DishRuleError

from dish_tool.step9 import recover_operation
from tests.support.verification import Backend, TASK, make_app


def _review(app, run: str = "review", agent: str = "codex"):
    result = app.execute(
        "start", agent=agent, task_gid="t", kind="verification", run_id=run,
        independence_attestation="independent",
    )
    assert result["ok"]
    inspected = app.execute("inspect", agent=agent, submission_id=result["submission_id"])
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result


def _approve_and_submit(app, operation_id: str, run: str = "review"):
    review = _review(app, run)
    approved = app.execute(
        "approve", model="gpt-5.6-sol", agent="codex", submission_id=operation_id, correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True, provenance_complete=True, run_id=run,
    )
    assert approved["ok"]
    submitted = app.execute("submit", submission_id=operation_id)
    assert submitted["ok"]
