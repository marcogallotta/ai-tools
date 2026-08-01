import copy
import json
import uuid
from pathlib import Path

import pytest


import dish_tool.application_service as application_service
import dish_tool.step8 as step8
from dish_service.leases import ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.database import content_identity, initialize_database
from dish_tool.database_schema import _validate_semantic_evidence
from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now
from tests.support.service_foundation import _service
from tests.support.verification import Backend, TASK, make_app
from tests.support.small_correction import (
    review_and_inspect as _review_and_inspect,
    small_candidate as _small_candidate,
    without_replay_marker as _without_replay_marker,
)







def _approve_small_correction(app, tmp_path, operation_id):
    review, inspected = _review_and_inspect(app, operation_id)
    cycle_before = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    approved = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="small",
        file_path=str(_small_candidate(tmp_path)),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="dish-020-review",
    )
    assert approved["ok"]
    return approved, inspected["data"]["dish_inspect_fact"], cycle_before


def _assert_small_correction_write_lineage(
    app, operation_id, approved, inspected_fact, cycle_before
):
    reviewed_identity = cycle_before["reviewed_identity"]
    corrected_identity = approved["data"]["approved_candidate_identity"]
    signed_identity = approved["data"]["signed_identity"]
    assert len({reviewed_identity, corrected_identity, signed_identity}) == 3
    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()
    fact = app.conn.execute(
        "SELECT * FROM dish_inspect_facts WHERE fact_id=?",
        (inspected_fact["fact_id"],),
    ).fetchone()
    assert cycle["reviewed_identity"] == fact["reviewed_identity"] == reviewed_identity
    assert cycle["reviewed_content_version_id"] == cycle_before["reviewed_content_version_id"]
    assert fact["reviewed_content_version_id"] == cycle_before["reviewed_content_version_id"]
    assert cycle["signed_identity"] == signed_identity

    correction_write = app.conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND purpose='content_write' AND outcome='confirmed'
               AND expected_identity=? AND intended_identity=?
             ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (operation_id, reviewed_identity, corrected_identity),
    ).fetchone()
    assert correction_write is not None
    corrected_version = app.conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id=?",
        (correction_write["confirmed_content_version_id"],),
    ).fetchone()
    assert corrected_version["identity"] == corrected_identity

    signoff_write = app.conn.execute(
        """SELECT * FROM write_attempts
             WHERE operation_id=? AND purpose='signoff' AND outcome='confirmed'
             ORDER BY started_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    assert signoff_write["expected_identity"] == corrected_identity
    assert signoff_write["intended_identity"] == signed_identity
    assert signoff_write["confirmed_content_version_id"] == cycle["signed_content_version_id"]
    assert json.loads(signoff_write["context_json"]) == {
        "correction_class": "small",
        "cycle_id": cycle["cycle_id"],
    }
    return cycle, fact, corrected_version


def _assert_small_correction_audit(app, operation_id, cycle, fact, corrected_version):
    audit = app.conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='verification.approved'
             ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    assert json.loads(audit["details"]) == {
        "approved_candidate_content_version_id": corrected_version["content_version_id"],
        "approved_candidate_identity": corrected_version["identity"],
        "correction_class": "small",
        "cycle_id": cycle["cycle_id"],
        "reviewed_content_version_id": fact["reviewed_content_version_id"],
        "reviewed_identity": fact["reviewed_identity"],
        "signed_content_version_id": cycle["signed_content_version_id"],
        "signed_identity": cycle["signed_identity"],
    }


def _assert_small_correction_survives_restart(tmp_path, cycle, fact):
    restarted = initialize_database(tmp_path / "dish.db")
    try:
        _validate_semantic_evidence(restarted)
        restarted_cycle = restarted.execute(
            "SELECT * FROM verification_cycles WHERE cycle_id=?", (cycle["cycle_id"],)
        ).fetchone()
        restarted_fact = restarted.execute(
            "SELECT * FROM dish_inspect_facts WHERE fact_id=?", (fact["fact_id"],)
        ).fetchone()
        assert restarted_cycle["reviewed_identity"] == cycle["reviewed_identity"]
        assert restarted_cycle["signed_identity"] == cycle["signed_identity"]
        assert restarted_fact["reviewed_identity"] == fact["reviewed_identity"]
    finally:
        restarted.close()


def test_small_correction_preserves_reviewed_fact_and_proves_full_lineage(tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)
    approved, inspected_fact, cycle_before = _approve_small_correction(
        app, tmp_path, operation_id
    )
    cycle, fact, corrected_version = _assert_small_correction_write_lineage(
        app, operation_id, approved, inspected_fact, cycle_before
    )
    _assert_small_correction_audit(app, operation_id, cycle, fact, corrected_version)
    _validate_semantic_evidence(app.conn)
    assert app.execute("sections", agent="gpt")["ok"]
    app.conn.close()
    _assert_small_correction_survives_restart(tmp_path, cycle, fact)


def test_small_correction_approval_replays_exactly_across_restart(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    constructor_run = str(uuid.uuid4())
    constructor = ServicePrincipal(owner_id="local:gpt", run_id=constructor_run)
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": constructor_run},
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "file_text": TASK,
        },
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert prepared["ok"]

    verifier_run = str(uuid.uuid4())
    verifier = ServicePrincipal(owner_id="local:codex", run_id=verifier_run)
    review = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "run_id": verifier_run,
            "independence_attestation": "independent",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert review["ok"]
    assert service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )["ok"]

    approval_request_id = str(uuid.uuid4())
    arguments = {
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "correction": "small",
        "file_text": TASK.replace("1. Cook it.", "1. Cook it gently."),
        "reviewed_identity": review["data"]["reviewed_identity"],
        "semantic_review_complete": True,
        "provenance_complete": True,
        "run_id": verifier_run,
    }
    approved = service.execute_agent(
        "approve",
        arguments,
        principal=verifier,
        request_id=approval_request_id,
    )
    assert approved["ok"]

    replay = service.execute_agent(
        "approve",
        arguments,
        principal=verifier,
        request_id=approval_request_id,
    )
    assert replay["ok"] and replay["data"]["request_replayed"] is True
    assert _without_replay_marker(replay) == approved

    restarted = _service(tmp_path, backend)
    replay_after_restart = restarted.execute_agent(
        "approve",
        arguments,
        principal=verifier,
        request_id=approval_request_id,
    )
    assert replay_after_restart["ok"]
    assert replay_after_restart["data"]["request_replayed"] is True
    assert _without_replay_marker(replay_after_restart) == approved
    assert restarted.execute_agent(
        "sections", {"agent": "gpt"}, principal=constructor
    )["ok"]
def test_approval_postcondition_cannot_return_ok_on_semantic_failure(tmp_path, monkeypatch):
    app, _backend, operation_id, _ = make_app(tmp_path)
    review, _ = _review_and_inspect(app, operation_id, run_id="postcondition")

    def fail_semantics(_conn):
        raise DishRuleError(
            "VALIDATION_FAILED",
            "database durable evidence is semantically inconsistent",
            rule="database_semantic_evidence_invalid",
            details={
                "problems": [
                    {
                        "invariant": "dish_inspect_fact_binding",
                        "record_type": "dish_inspect_facts",
                        "record_id": "synthetic-fact",
                    }
                ],
                "problem_count": 1,
            },
        )

    monkeypatch.setattr(application_service, "_validate_semantic_evidence", fail_semantics)
    result = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="small",
        file_path=str(_small_candidate(tmp_path)),
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="postcondition",
    )

    assert result["ok"] is False
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["data"]["original_failure_rule"] == "database_semantic_evidence_invalid"
    execution = app.conn.execute(
        """SELECT status,evidence_json FROM operation_executions
             WHERE operation_id=? AND command='approve'
             ORDER BY rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    assert execution["status"] == "uncertain"
    assert json.loads(execution["evidence_json"])["failed_step"] == "database_semantic_evidence_invalid"
