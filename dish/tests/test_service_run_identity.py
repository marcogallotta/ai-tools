import json

from dish_service.leases import ServicePrincipal
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from tests.support.service_foundation import _release_loader
from tests.support.service_leases import _service
from tests.support.verification import Backend, TASK


def _principal(owner: str, run_id: str) -> ServicePrincipal:
    return ServicePrincipal(owner, run_id)


def _start_and_prepare(service, *, run_id: str = "constructor-run"):
    principal = _principal("constructor", run_id)
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
    )
    assert started["ok"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=principal,
    )
    assert prepared["ok"]
    return started["submission_id"]


def _start_verification(service, operation_id: str, *, run_id: str = "verifier-run"):
    principal = _principal("verifier", run_id)
    review = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "independence_attestation": "independent"},
        principal=principal,
    )
    assert review["ok"]
    assert review["submission_id"] == operation_id
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=principal,
    )
    assert inspected["ok"]
    return principal, review


def _actor_provenance(row) -> dict[str, object]:
    return json.loads(row["actor_provenance"])


def test_client_run_id_persists_at_start_prepare_and_inspect(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    operation_id = _start_and_prepare(service)

    inspected = service.execute_agent(
        "inspect",
        {"agent": "gpt", "submission_id": operation_id},
        principal=_principal("constructor", "constructor-run"),
    )

    assert inspected["ok"]
    assert inspected["data"]["operation"]["run_id"] == "constructor-run"
    assert inspected["data"]["actors"]["run_id"] == "constructor-run"
    conn = initialize_database(service.config.db_path)
    try:
        actor = conn.execute(
            """SELECT role,run_id FROM operation_actor_facts
                 WHERE operation_id=? AND role='constructor'
                 ORDER BY created_at LIMIT 1""",
            (operation_id,),
        ).fetchone()
        audits = conn.execute(
            """SELECT event_type,actor_provenance FROM audit_events
                 WHERE operation_id=? AND event_type IN ('operation.created','dish.start','dish.prepare')
                 ORDER BY created_at""",
            (operation_id,),
        ).fetchall()
    finally:
        conn.close()
    assert tuple(actor) == ("constructor", "constructor-run")
    assert {row["event_type"] for row in audits} == {
        "operation.created", "dish.start", "dish.prepare",
    }
    assert all(_actor_provenance(row)["run_id"] == "constructor-run" for row in audits)


def test_authenticated_client_rejects_conflicting_later_command_run_id(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    principal = _principal("constructor", "client-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
    )
    assert started["ok"]

    result = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
            "run_id": "different-run",
        },
        principal=principal,
    )

    assert result["code"] == "AGENT_MISMATCH"
    assert result["task_gid"] == "t"
    assert result["submission_id"] == started["submission_id"]
    assert result["errors"][0]["rule"] == "service_run_id_conflict"
    assert backend.writes == 0
    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT run_id FROM operations WHERE operation_id=?",
            (started["submission_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert operation["run_id"] == "client-run"


def test_null_run_id_remains_null_without_client_identity(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    app = DishApplication(
        initialize_database(tmp_path / "local.db"),
        backend,
        release_loader=_release_loader(honest),
    )

    started = app.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
    )

    assert started["ok"]
    inspected = app.execute(
        "inspect", agent="gpt", submission_id=started["submission_id"],
    )
    assert inspected["data"]["operation"]["run_id"] is None
    assert inspected["data"]["actors"]["run_id"] is None
    actor = app.conn.execute(
        """SELECT run_id FROM operation_actor_facts
             WHERE operation_id=? AND role='constructor'""",
        (started["submission_id"],),
    ).fetchone()
    assert actor["run_id"] is None
    audit = app.conn.execute(
        """SELECT actor_provenance FROM audit_events
             WHERE operation_id=? AND event_type='dish.start'""",
        (started["submission_id"],),
    ).fetchone()
    assert _actor_provenance(audit)["run_id"] is None


def test_fresh_verifier_run_persists_through_approve_and_submit(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    operation_id = _start_and_prepare(service)
    verifier, review = _start_verification(service, operation_id)
    approved = service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "correction": "none",
            "reviewed_identity": review["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
        },
        principal=verifier,
    )
    assert approved["ok"]
    submitted = service.execute_agent(
        "submit", {"submission_id": operation_id}, principal=verifier,
    )
    assert submitted["ok"]

    inspected = service.execute_agent(
        "inspect",
        {"agent": "gpt", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["data"]["operation"]["run_id"] == "constructor-run"
    assert inspected["data"]["actors"]["run_id"] == "constructor-run"
    cycle = inspected["data"]["verification_cycles"][0]
    assert cycle["run_id"] == "verifier-run"
    assert cycle["cycle_id"] != operation_id

    conn = initialize_database(service.config.db_path)
    try:
        verifier_fact = conn.execute(
            """SELECT run_id,source_cycle_id FROM operation_actor_facts
                 WHERE operation_id=? AND role='verifier'""",
            (operation_id,),
        ).fetchone()
        audits = conn.execute(
            """SELECT event_type,actor_provenance FROM audit_events
                 WHERE operation_id=? AND event_type IN (
                     'verification.review_started','dish.start','dish.approve','dish.submit'
                 ) ORDER BY created_at""",
            (operation_id,),
        ).fetchall()
    finally:
        conn.close()
    assert tuple(verifier_fact) == ("verifier-run", cycle["cycle_id"])
    verifier_audits = [
        row for row in audits
        if _actor_provenance(row)["run_id"] == "verifier-run"
    ]
    assert {"verification.review_started", "dish.start", "dish.approve", "dish.submit"} <= {
        row["event_type"] for row in verifier_audits
    }


def test_large_correction_preserves_constructor_and_fresh_verifier_runs(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    operation_id = _start_and_prepare(service)
    first_verifier, _review = _start_verification(
        service, operation_id, run_id="first-verifier",
    )
    candidate = TASK.replace("100 g test ingredient", "120 g test ingredient")
    rejected = service.execute_agent(
        "reject",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": operation_id,
            "route": "large",
            "reason": "material correction",
            "file_text": candidate,
        },
        principal=first_verifier,
    )
    assert rejected["ok"]
    second_verifier, second_review = _start_verification(
        service, operation_id, run_id="second-verifier",
    )
    assert second_review["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        facts = conn.execute(
            """SELECT role,run_id,source_cycle_id FROM operation_actor_facts
                 WHERE operation_id=?
                 ORDER BY created_at,fact_id""",
            (operation_id,),
        ).fetchall()
        cycles = conn.execute(
            """SELECT cycle_id,run_id,outcome FROM verification_cycles
                 WHERE operation_id=? ORDER BY cycle_number""",
            (operation_id,),
        ).fetchall()
        reject_audit = conn.execute(
            """SELECT actor_provenance FROM audit_events
                 WHERE operation_id=? AND event_type='dish.reject'
                 ORDER BY created_at DESC LIMIT 1""",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert ("constructor", "constructor-run") in {
        (row["role"], row["run_id"]) for row in facts
    }
    assert ("material_editor", "first-verifier") in {
        (row["role"], row["run_id"]) for row in facts
    }
    assert ("verifier", "first-verifier") in {
        (row["role"], row["run_id"]) for row in facts
    }
    assert ("verifier", "second-verifier") in {
        (row["role"], row["run_id"]) for row in facts
    }
    assert [row["run_id"] for row in cycles] == [
        "first-verifier", "second-verifier",
    ]
    assert cycles[0]["outcome"] == "rejected"
    assert cycles[0]["cycle_id"] != cycles[1]["cycle_id"]
    assert _actor_provenance(reject_audit)["run_id"] == "first-verifier"
