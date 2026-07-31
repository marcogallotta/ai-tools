import copy
import json
import sys
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(BIN), str(TESTS)]

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


def _review_and_inspect(app, operation_id: str, *, run_id: str = "dish-020-review"):
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id=run_id,
        independence_attestation="independent",
    )
    assert review["ok"]
    inspected = app.execute(
        "inspect", agent="codex", submission_id=operation_id
    )
    assert inspected["ok"]
    return review, inspected


def _small_candidate(path: Path) -> Path:
    candidate = path / "small-correction.txt"
    candidate.write_text(
        TASK.replace("1. Cook it.", "1. Cook it gently."), encoding="utf-8"
    )
    return candidate


def _without_replay_marker(result):
    normalized = copy.deepcopy(result)
    normalized.get("data", {}).pop("request_replayed", None)
    return normalized


def test_small_correction_preserves_reviewed_fact_and_proves_full_lineage(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    review, inspected = _review_and_inspect(app, operation_id)
    inspected_fact = inspected["data"]["dish_inspect_fact"]
    reviewed_identity = review["data"]["reviewed_identity"]
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
        reviewed_identity=reviewed_identity,
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="dish-020-review",
    )

    assert approved["ok"]
    corrected_identity = approved["data"]["approved_candidate_identity"]
    signed_identity = approved["data"]["signed_identity"]
    assert len({reviewed_identity, corrected_identity, signed_identity}) == 3

    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    fact = app.conn.execute(
        "SELECT * FROM dish_inspect_facts WHERE fact_id=?",
        (inspected_fact["fact_id"],),
    ).fetchone()
    assert cycle["reviewed_identity"] == reviewed_identity
    assert cycle["reviewed_content_version_id"] == cycle_before["reviewed_content_version_id"]
    assert fact["reviewed_identity"] == reviewed_identity
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

    audit = app.conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='verification.approved'
             ORDER BY created_at DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    details = json.loads(audit["details"])
    assert details == {
        "approved_candidate_content_version_id": corrected_version["content_version_id"],
        "approved_candidate_identity": corrected_identity,
        "correction_class": "small",
        "cycle_id": cycle["cycle_id"],
        "reviewed_content_version_id": fact["reviewed_content_version_id"],
        "reviewed_identity": reviewed_identity,
        "signed_content_version_id": cycle["signed_content_version_id"],
        "signed_identity": signed_identity,
    }

    _validate_semantic_evidence(app.conn)
    unrelated = app.execute("sections", agent="gpt")
    assert unrelated["ok"]

    app.conn.close()
    restarted = initialize_database(tmp_path / "dish.db")
    try:
        _validate_semantic_evidence(restarted)
        restarted_cycle = restarted.execute(
            "SELECT * FROM verification_cycles WHERE cycle_id=?", (cycle["cycle_id"],)
        ).fetchone()
        restarted_fact = restarted.execute(
            "SELECT * FROM dish_inspect_facts WHERE fact_id=?", (fact["fact_id"],)
        ).fetchone()
        assert restarted_cycle["reviewed_identity"] == reviewed_identity
        assert restarted_cycle["signed_identity"] == signed_identity
        assert restarted_fact["reviewed_identity"] == reviewed_identity
    finally:
        restarted.close()


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


def test_restarted_small_signoff_recovery_uses_same_semantic_completion_gate(
    tmp_path, monkeypatch
):
    app, backend, operation_id, _ = make_app(tmp_path)
    review, _ = _review_and_inspect(app, operation_id, run_id="recovery-boundary")

    def interrupt_before_signoff(*_args, **_kwargs):
        raise RuntimeError("simulated process loss before Small signoff")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(step8, "approve_live", interrupt_before_signoff)
        failed = app.execute(
            "approve",
            agent="codex",
            model="gpt-5.6-sol",
            submission_id=operation_id,
            correction="small",
            file_path=str(_small_candidate(tmp_path)),
            reviewed_identity=review["data"]["reviewed_identity"],
            semantic_review_complete=True,
            provenance_complete=True,
            run_id="recovery-boundary",
        )
    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["data"]["pending_steps"] == ["small_signoff"]

    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=app.release_loader
    )

    def fail_semantics(_conn):
        raise DishRuleError(
            "VALIDATION_FAILED",
            "database durable evidence is semantically inconsistent",
            rule="database_semantic_evidence_invalid",
            details={"problems": [], "problem_count": 1},
        )

    monkeypatch.setattr(application_service, "_validate_semantic_evidence", fail_semantics)
    recovered = admin.execute(
        "recover",
        submission_id=operation_id,
        outcome="applied",
        reason="resume interrupted Small-correction signoff",
    )
    assert recovered["ok"] is False
    assert recovered["code"] == "BACKEND_UNCERTAIN"
    assert recovered["data"]["original_failure_rule"] == "database_semantic_evidence_invalid"
    assert recovered["data"]["command"] == "recover"



def test_existing_bad_inspect_binding_is_diagnosed_precisely_without_repair(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    _review_and_inspect(app, operation_id, run_id="forensic-copy")
    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()
    fact = app.conn.execute(
        "SELECT * FROM dish_inspect_facts WHERE cycle_id=?", (cycle["cycle_id"],)
    ).fetchone()

    disguised_notes = backend.notes + "forensic-copy-only\n"
    displaced_identity = content_identity(backend.title, disguised_notes).digest
    displaced_version_id = str(uuid.uuid4())
    app.conn.execute(
        """INSERT INTO content_versions(
               content_version_id,task_gid,operation_id,boundary,identity,title,notes,
               confirmed,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            displaced_version_id,
            "t",
            operation_id,
            "content_write",
            displaced_identity,
            backend.title,
            disguised_notes,
            1,
            utc_now(),
        ),
    )
    app.conn.execute(
        """UPDATE verification_cycles
              SET correction_class='small', reviewed_content_version_id=?, reviewed_identity=?
            WHERE cycle_id=?""",
        (displaced_version_id, displaced_identity, cycle["cycle_id"]),
    )

    with pytest.raises(DishRuleError) as raised:
        _validate_semantic_evidence(app.conn)
    assert raised.value.rule == "database_semantic_evidence_invalid"
    details = raised.value.details
    assert details["problem_count"] == 1
    assert details["diagnostic_timestamp"].endswith("Z")
    assert details["transaction_state"] == {
        "connection_in_transaction": False,
        "evidence_visibility": "committed_database",
    }
    problem = details["problems"][0]
    assert problem["invariant"] == "dish_inspect_fact_binding"
    assert problem["record_type"] == "dish_inspect_facts"
    assert problem["record_id"] == fact["fact_id"]
    assert problem["mutation_provenance"] == {
        "task_gid": "t",
        "operation_id": operation_id,
        "run_id": "forensic-copy",
        "cycle_id": cycle["cycle_id"],
    }
    assert problem["timestamps"] == {"created_at": fact["created_at"]}
    relationship = problem["broken_relationship"]
    assert relationship["source_fields"] == [
        "cycle_id",
        "operation_id",
        "task_gid",
        "reviewed_content_version_id",
        "reviewed_identity",
        "verifier_agent",
        "run_id",
        "independence_attestation",
    ]
    assert [target["record_type"] for target in relationship["targets"]] == [
        "verification_cycles",
        "content_versions",
        "operation_actor_facts",
    ]
    assert relationship["required_predicate"] == (
        "inspect fact exactly matches its cycle, confirmed reviewed content version, "
        "and verifier actor fact"
    )
    still_displaced = app.conn.execute(
        "SELECT reviewed_content_version_id,reviewed_identity FROM verification_cycles WHERE cycle_id=?",
        (cycle["cycle_id"],),
    ).fetchone()
    assert tuple(still_displaced) == (displaced_version_id, displaced_identity)
    persisted_fact = app.conn.execute(
        "SELECT reviewed_content_version_id,reviewed_identity FROM dish_inspect_facts WHERE fact_id=?",
        (fact["fact_id"],),
    ).fetchone()
    assert tuple(persisted_fact) == (
        fact["reviewed_content_version_id"], fact["reviewed_identity"]
    )


def test_no_correction_large_rejection_and_stale_candidate_rules_remain_intact(tmp_path):
    no_correction_dir = tmp_path / "none"
    no_correction_dir.mkdir()
    app, _backend, operation_id, _ = make_app(no_correction_dir)
    review, inspected = _review_and_inspect(app, operation_id, run_id="none")
    approved = app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        correction="none",
        reviewed_identity=review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="none",
    )
    assert approved["ok"]
    cycle = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=?", (operation_id,)
    ).fetchone()
    assert cycle["reviewed_identity"] == inspected["data"]["dish_inspect_fact"]["reviewed_identity"]
    assert cycle["correction_class"] == "none"
    _validate_semantic_evidence(app.conn)

    large_dir = tmp_path / "large"
    large_dir.mkdir()
    large_app, _large_backend, large_operation_id, _ = make_app(large_dir)
    _large_review, large_inspected = _review_and_inspect(
        large_app, large_operation_id, run_id="large"
    )
    large_candidate = large_dir / "large.txt"
    large_candidate.write_text(TASK.replace("100 g", "120 g"), encoding="utf-8")
    rejected = large_app.execute(
        "reject",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=large_operation_id,
        route="large",
        reason="material quantity correction requires another review",
        file_path=str(large_candidate),
        run_id="large",
    )
    assert rejected["ok"]
    old_cycle = large_app.conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=?",
        (large_inspected["data"]["dish_inspect_fact"]["cycle_id"],),
    ).fetchone()
    assert old_cycle["reviewed_identity"] == large_inspected["data"]["dish_inspect_fact"]["reviewed_identity"]
    new_cycle = large_app.conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND completed_at IS NULL
             ORDER BY cycle_number DESC LIMIT 1""",
        (large_operation_id,),
    ).fetchone()
    assert new_cycle["cycle_id"] != old_cycle["cycle_id"]
    assert large_app.conn.execute(
        "SELECT COUNT(*) FROM dish_inspect_facts WHERE cycle_id=?", (new_cycle["cycle_id"],)
    ).fetchone()[0] == 0
    _validate_semantic_evidence(large_app.conn)

    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    stale_app, stale_backend, stale_operation_id, _ = make_app(stale_dir)
    stale_review, _ = _review_and_inspect(stale_app, stale_operation_id, run_id="stale")
    stale_backend.title += " externally changed"
    stale = stale_app.execute(
        "approve",
        agent="codex",
        model="gpt-5.6-sol",
        submission_id=stale_operation_id,
        correction="none",
        reviewed_identity=stale_review["data"]["reviewed_identity"],
        semantic_review_complete=True,
        provenance_complete=True,
        run_id="stale",
    )
    assert stale["code"] == "CONFLICT"
    assert stale["errors"][0]["rule"] in {"live_task_drift", "stale_verifier_review"}
