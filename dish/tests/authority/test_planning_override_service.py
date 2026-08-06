"""SQLite/service proof that planning confirmation is not mutation authority.

The unrelated-field check is intentionally limited to the Action request contract.
"""
from __future__ import annotations

import uuid

import pytest

from dish_service.command_spec import validate_action_request
from dish_tool.commands import DishApplication
from dish_tool.database import reserve_marco_authorizations
from dish_tool.errors import DishRuleError
from tests.support.asana_backend import StatefulAsanaBackend
from tests.support.planning_intent import (
    RUN_ID,
    TASK_GID,
    confirm,
    connect,
    issue,
    principal,
    service as planning_service,
)
from tests.support.planning import PLANNING
from tests.support.verification import TASK, review_and_inspect

OVERRIDE_REASON = "Bounded proactive planning was explicitly selected"
CHANGE = {"field": "Locks", "before": "Keep crisp", "after": "Use whole scallion"}


def _service(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    backend = StatefulAsanaBackend(
        task_gid=TASK_GID,
        tasks=(
            {"task_gid": TASK_GID, "title": "Planning", "notes": "", "section_gid": "rq"},
        ),
    )
    return planning_service(tmp_path, backend=backend)


def _confirm(service, basis):
    challenge = issue(service)
    started = confirm(
        service,
        challenge,
        intent_basis=basis,
        override_reason=OVERRIDE_REASON if basis == "agent_override" else None,
    )
    assert started["ok"], started
    return challenge, started


def _governed_requirement(service, operation_id):
    conn = connect(service)
    try:
        with pytest.raises(DishRuleError) as caught:
            reserve_marco_authorizations(
                conn, task_gid=TASK_GID, operation_id=operation_id, changes=(CHANGE,)
            )
        error = caught.value
        assert error.rule == "governed_change_unauthorized"
        return (
            error.code,
            error.rule,
            error.details["required_admin_action"],
            error.details["missing_authorizations"],
        )
    finally:
        conn.close()


def _pending_proposal(service, backend, tmp_path):
    conn = connect(service)
    app = DishApplication(conn, backend, release_loader=service.release_loader)
    candidate = tmp_path / "proposal.txt"
    candidate.write_text(TASK, encoding="utf-8")
    started = app.execute(
        "start", agent="gpt", task_gid=TASK_GID, kind="initial", run_id="constructor"
    )
    assert app.execute(
        "prepare", agent="gpt", model="m", submission_id=started["submission_id"],
        file_path=str(candidate), run_id="constructor",
    )["ok"]
    review_and_inspect(
        app, agent="codex", task_gid=TASK_GID, run_id="reviewer",
        operation_id=started["submission_id"],
    )
    candidate.write_text(
        TASK.replace("Locks: Keep crisp", "Locks: Keep crisp | Use whole scallion"),
        encoding="utf-8",
    )
    queued = app.execute(
        "reject", agent="codex", model="m", submission_id=started["submission_id"],
        route="large", reason="Apply one exact linked governed correction",
        file_path=str(candidate), run_id="reviewer",
    )
    assert queued["data"]["proposal_status"] == "pending", queued
    return app, queued["data"]["proposal_id"]


def test_override_creates_no_authority_and_preserves_governed_gate(tmp_path):
    baseline, _ = _service(tmp_path / "baseline")
    _, baseline_start = _confirm(baseline, "user_requested")
    expected = _governed_requirement(baseline, baseline_start["submission_id"])

    service, _ = _service(tmp_path / "override")
    challenge, started = _confirm(service, "agent_override")
    assert _governed_requirement(service, started["submission_id"]) == expected

    conn = connect(service)
    try:
        row = conn.execute(
            "SELECT status,intent_basis,override_reason FROM planning_intent_challenges "
            "WHERE challenge_id=?",
            (challenge["data"]["intent_challenge_id"],),
        ).fetchone()
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM marco_authorizations),"
            "(SELECT COUNT(*) FROM verification_cycles WHERE route='human_review'),"
            "(SELECT COUNT(*) FROM operation_actor_facts WHERE role='human'),"
            "(SELECT COUNT(*) FROM semantic_proposals "
            " WHERE status IN ('approved','claimed','applied'))"
        ).fetchone()
        assert tuple(row) == ("consumed", "agent_override", OVERRIDE_REASON)
        assert tuple(counts) == (0, 0, 0, 0)
    finally:
        conn.close()


def test_override_does_not_make_pending_proposal_applicable(tmp_path):
    service, backend = _service(tmp_path)
    _, planning = _confirm(service, "agent_override")
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "m",
            "submission_id": planning["submission_id"],
            "file_text": PLANNING,
        },
        principal=principal(),
        request_id=str(uuid.uuid4()),
    )
    assert prepared["ok"], prepared
    app, proposal_id = _pending_proposal(service, backend, tmp_path)
    try:
        blocked = app.execute(
            "apply-proposal", proposal_id=proposal_id, agent="gpt", model="m",
            run_id="applicant",
        )
        assert blocked["errors"][0]["rule"] == "semantic_proposal_not_claimable"
        assert app.conn.execute(
            "SELECT status FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()[0] == "pending"
        assert app.conn.execute("SELECT COUNT(*) FROM marco_authorizations").fetchone()[0] == 0
    finally:
        app.conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_challenge_id", "77777777-7777-4777-8777-777777777777"),
        ("intent_basis", "agent_override"),
        ("override_reason", "not mutation authority"),
    ],
)
def test_action_inspect_rejects_unrelated_planning_fields(field, value):
    with pytest.raises(DishRuleError) as caught:
        validate_action_request(
            "inspect",
            {
                "client": {"run_id": RUN_ID, "request_id": str(uuid.uuid4())},
                "arguments": {
                    "agent": "codex",
                    "submission_id": "77777777-7777-4777-8777-777777777777",
                    field: value,
                },
            },
        )
    assert caught.value.rule == "argument_unexpected"
    assert caught.value.details["field"] == field
