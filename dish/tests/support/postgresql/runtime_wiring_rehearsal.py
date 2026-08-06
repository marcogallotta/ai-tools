"""Shared mechanics for the committed §3 native runtime-wiring rehearsal."""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_pg.workflow import WorkflowAuthorityService
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import NOW, _bootstrap_registry
from tests.support.postgresql.process_failure import (
    BarrierServer,
    ChildProcess,
    event_snapshot,
    expire_claim,
    read_ledger,
    start_postgresql_proxy,
    start_postgresql_service,
    start_projection_worker,
    start_reconciliation_worker,
    write_scenario,
)

NODEID = (
    "tests/postgresql/native/test_runtime_wiring_rehearsal.py::"
    "test_runtime_wiring_rehearsal_across_service_and_worker_processes"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _json_request(
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return int(exc.code), json.loads(exc.read().decode("utf-8"))


def _wait_health(url: str, *, expected_ok: bool, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        try:
            status, payload = _json_request(url, timeout=2.0)
            last = (status, payload)
            if bool(payload.get("ok")) is expected_ok:
                return payload
        except (OSError, ValueError) as exc:
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"health did not reach ok={expected_ok}: last={last!r}")


def _command(
    base_url: str,
    token: str,
    *,
    command: str,
    run_id: uuid.UUID,
    request_id: uuid.UUID | None,
    arguments: dict,
) -> tuple[int, dict]:
    client = {"run_id": str(run_id)}
    if request_id is not None:
        client["request_id"] = str(request_id)
    return _json_request(
        f"{base_url}/v1/commands/{command}",
        token=token,
        body={"client": client, "arguments": arguments},
        timeout=15.0,
    )


def _process_record(child: ChildProcess) -> dict[str, Any]:
    return {
        "label": child.manifest["label"],
        "pid": child.manifest["pid"],
        "command": child.manifest["command"],
        "completion_state": child.manifest["completion_state"],
        "final_exit_status": child.manifest["final_exit_status"],
        "termination_state": child.manifest["termination_state"],
    }


def _create_task(
    *,
    factory,
    ids,
    base_url: str,
    token: str,
    run_id: uuid.UUID,
    title: str,
) -> dict[str, Any]:
    request_id = next(ids)
    status, result = _command(
        base_url,
        token,
        command="create",
        run_id=run_id,
        request_id=request_id,
        arguments={"title": title},
    )
    assert status == 200 and result["ok"] is True
    task_id = uuid.UUID(result["data"]["task_id"])
    with session_scope(factory) as session:
        event_ids = list(
            session.scalars(
                select(tx.ProjectionOutboxEvent.projection_event_id)
                .where(tx.ProjectionOutboxEvent.task_id == task_id)
                .order_by(tx.ProjectionOutboxEvent.aggregate_sequence)
            )
        )
    assert len(event_ids) == 1
    return {
        "request_id": request_id,
        "task_id": task_id,
        "event_ids": event_ids,
        "result": result,
    }


def _authoritative_confirmed_settlement(
    snapshot: dict[str, Any], *, attempt_id: str
) -> dict[str, Any]:
    attempt = next(row for row in snapshot["attempts"] if row["attempt_id"] == attempt_id)
    observations = [row for row in snapshot["observations"] if row["attempt_id"] == attempt_id]
    adjudications = [row for row in snapshot["adjudications"] if row["attempt_id"] == attempt_id]
    assert attempt["state"] == "confirmed" and attempt["terminal"] is True
    assert len(observations) == 1
    observation = observations[0]
    external = observation["evidence"]["external_observation"]
    assert observation["kind"] == "marker_search"
    assert observation["observed_applied"] is True
    assert observation["reread_complete"] is True
    assert external["source"] == "external_marker_search"
    assert external["operation"] == "create_task"
    assert external["correlation_marker"]
    assert external["observed_external_id"] == attempt["intended_external_id"]
    assert len(adjudications) == 1
    adjudication = adjudications[0]
    assert adjudication["outcome"] == "confirmed"
    assert adjudication["decided_by"] == "automatic"
    assert snapshot["correlations"] == [
        {
            "event_id": snapshot["events"][0]["event_id"],
            "marker": external["correlation_marker"],
            "state": "bound",
            "matched_external_id": attempt["intended_external_id"],
            "match_count": 1,
        }
    ]
    return {
        "attempt": attempt,
        "authoritative_external_observation": observation,
        "settlement_adjudication": adjudication,
        "correlation": snapshot["correlations"][0],
    }


def _exercise_unsupported_routes(
    *,
    private_base_url: str,
    action_base_url: str,
    run_id: uuid.UUID,
    ids,
    agent_token: str,
    admin_token: str,
    action_token: str,
) -> dict[str, Any]:
    operation_id = next(ids)
    cases = (
        (
            f"{private_base_url}/v1/leases/{operation_id}/renew",
            agent_token,
            {"client": {"run_id": str(run_id), "request_id": str(next(ids))}},
        ),
        (
            f"{private_base_url}/v1/admin/leases/{operation_id}/recover",
            admin_token,
            {
                "client": {"run_id": str(run_id), "request_id": str(next(ids))},
                "reason": "must remain hidden in PostgreSQL TEST mode",
            },
        ),
        (
            f"{private_base_url}/v1/admin/inspect",
            admin_token,
            {
                "client": {"run_id": str(run_id), "request_id": str(next(ids))},
                "arguments": {},
            },
        ),
        (
            f"{action_base_url}/v1/action/renew-lease",
            action_token,
            {
                "client": {"run_id": str(run_id), "request_id": str(next(ids))},
                "arguments": {"operation_id": str(operation_id)},
            },
        ),
    )
    results = []
    for url, token, body in cases:
        status, payload = _json_request(url, token=token, body=body)
        assert status == 404
        assert payload == {"ok": False, "error": "not_found"}
        results.append({"url": url, "status": status, "payload": payload})
    return {"status": "passed", "routes": results, "internal_error_count": 0}


def _exercise_same_worker_restart(
    *,
    factory,
    ids,
    base_url: str,
    token: str,
    run_id: uuid.UUID,
    dsn: str,
    tmp_path: Path,
    identity_dir: Path,
    identity_expectations: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Path]]:
    created = _create_task(
        factory=factory,
        ids=ids,
        base_url=base_url,
        token=token,
        run_id=run_id,
        title="Runtime wiring same-worker restart",
    )
    event_ids = created["event_ids"]
    ledger = tmp_path / "same-worker-restart-ledger.json"
    worker_id = "section3-restart"
    before_identity = identity_dir / "projection-restart-before-death-identity.json"
    after_identity = identity_dir / "projection-restart-after-death-identity.json"
    noop_identity = identity_dir / "projection-restart-noop-identity.json"

    with BarrierServer() as barrier:
        before_death = start_projection_worker(
            dsn=dsn,
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id=worker_id,
            process_label="projection-section3-restart-before-death",
            scenario="ambiguous_response",
            barrier=barrier,
            identity_output=before_identity,
            **identity_expectations,
        )
        reached = barrier.wait("after_ambiguous_external_response_before_settlement")
        snapshot_before_death = event_snapshot(factory, event_ids)
        ledger_before_death = read_ledger(ledger)
        assert reached.pid == before_death.process.pid
        assert snapshot_before_death["events"][0]["state"] == "claimed"
        assert [(row["kind"], row["state"], row["worker_id"]) for row in snapshot_before_death["attempts"]] == [
            ("dispatch", "dispatched", worker_id)
        ]
        assert snapshot_before_death["observation_count"] == 0
        assert snapshot_before_death["adjudication_count"] == 0
        assert ledger_before_death["dispatch_calls"] == 1
        before_death.kill()
        reached.close()

    expire_claim(factory, event_ids[0])
    after_restart = start_projection_worker(
        dsn=dsn,
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id=worker_id,
        process_label="projection-section3-restart-after-death",
        identity_output=after_identity,
        **identity_expectations,
    )
    after_restart.wait()
    snapshot_after_restart = event_snapshot(factory, event_ids)
    ledger_after_restart = read_ledger(ledger)
    attempts = snapshot_after_restart["attempts"]
    assert snapshot_after_restart["events"][0]["state"] == "applied"
    assert [(row["kind"], row["state"], row["worker_id"], row["terminal"]) for row in attempts] == [
        ("dispatch", "uncertain", worker_id, True),
        ("recovery", "confirmed", worker_id, True),
    ]
    assert attempts[1]["predecessor_attempt_id"] == attempts[0]["attempt_id"]
    dispatch_observation = next(
        row for row in snapshot_after_restart["observations"] if row["attempt_id"] == attempts[0]["attempt_id"]
    )
    dispatch_adjudication = next(
        row for row in snapshot_after_restart["adjudications"] if row["attempt_id"] == attempts[0]["attempt_id"]
    )
    assert dispatch_observation["kind"] == "preflight"
    assert dispatch_observation["observed_applied"] is None
    assert dispatch_observation["evidence"]["external_observation"]["source"] == "unavailable"
    assert dispatch_adjudication["outcome"] == "uncertain"
    settlement = _authoritative_confirmed_settlement(
        snapshot_after_restart, attempt_id=attempts[1]["attempt_id"]
    )
    assert ledger_after_restart["dispatch_calls"] == 1
    assert ledger_after_restart["recovery_observations"] == 1

    post_settlement_restart = start_projection_worker(
        dsn=dsn,
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id=worker_id,
        process_label="projection-section3-restart-noop",
        identity_output=noop_identity,
        **identity_expectations,
    )
    post_settlement_restart.wait()
    snapshot_after_noop = event_snapshot(factory, event_ids)
    ledger_after_noop = read_ledger(ledger)
    assert snapshot_after_noop == snapshot_after_restart
    assert ledger_after_noop == ledger_after_restart
    assert before_death.process.pid != after_restart.process.pid
    assert after_restart.process.pid != post_settlement_restart.process.pid

    evidence = {
        "status": "passed",
        "worker_id": worker_id,
        "command_boundary": {
            "request_id": str(created["request_id"]),
            "task_id": str(created["task_id"]),
            "result": created["result"],
        },
        "before_process_death": {
            "process": _process_record(before_death),
            "snapshot": snapshot_before_death,
            "external_ledger": ledger_before_death,
        },
        "after_same_logical_worker_restart": {
            "process": _process_record(after_restart),
            "snapshot": snapshot_after_restart,
            "external_ledger": ledger_after_restart,
            "settlement": settlement,
        },
        "no_duplicate_after_post_settlement_restart": {
            "process": _process_record(post_settlement_restart),
            "snapshot_unchanged": True,
            "external_ledger_unchanged": True,
            "snapshot": snapshot_after_noop,
            "external_ledger": ledger_after_noop,
        },
    }
    return evidence, {
        "restart_before": before_identity,
        "restart_after": after_identity,
        "restart_noop": noop_identity,
    }


def _exercise_takeover_and_stale_rejection(
    *,
    factory,
    ids,
    base_url: str,
    token: str,
    run_id: uuid.UUID,
    dsn: str,
    tmp_path: Path,
    identity_dir: Path,
    identity_expectations: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    created = _create_task(
        factory=factory,
        ids=ids,
        base_url=base_url,
        token=token,
        run_id=run_id,
        title="Runtime wiring different-worker takeover",
    )
    event_ids = created["event_ids"]
    ledger = tmp_path / "different-worker-takeover-ledger.json"
    original_worker_id = "section3-takeover-original"
    replacement_worker_id = "section3-takeover-replacement"
    original_identity = identity_dir / "projection-takeover-original-identity.json"
    replacement_identity = identity_dir / "projection-takeover-replacement-identity.json"
    noop_identity = identity_dir / "projection-takeover-noop-identity.json"

    with BarrierServer() as original_barrier:
        original = start_projection_worker(
            dsn=dsn,
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id=original_worker_id,
            process_label="projection-section3-takeover-original",
            scenario="after_claim",
            barrier=original_barrier,
            identity_output=original_identity,
            **identity_expectations,
        )
        reached_original = original_barrier.wait("after_claim_before_durable_intent")
        original_claim = event_snapshot(factory, event_ids)
        original_event = original_claim["events"][0]
        assert original_event["state"] == "claimed"
        assert original_event["claim_owner"] == original_worker_id
        assert original_claim["attempts"] == []
        expire_claim(factory, event_ids[0])

        with BarrierServer() as replacement_barrier:
            replacement = start_projection_worker(
                dsn=dsn,
                tmp_path=tmp_path,
                ledger=ledger,
                worker_id=replacement_worker_id,
                process_label="projection-section3-takeover-replacement",
                scenario="after_claim",
                barrier=replacement_barrier,
                identity_output=replacement_identity,
                **identity_expectations,
            )
            reached_replacement = replacement_barrier.wait("after_claim_before_durable_intent")
            takeover_claim = event_snapshot(factory, event_ids)
            takeover_event = takeover_claim["events"][0]
            assert takeover_event["state"] == "claimed"
            assert takeover_event["claim_owner"] == replacement_worker_id
            assert takeover_event["claim_revision"] > original_event["claim_revision"]
            assert takeover_claim["attempts"] == []

            reached_original.release()
            original.wait()
            original_log = original.log_path.read_text(encoding="utf-8")
            assert "projection claim lost mid-processing" in original_log
            after_stale_rejection = event_snapshot(factory, event_ids)
            assert after_stale_rejection == takeover_claim
            assert all(row["worker_id"] != original_worker_id for row in after_stale_rejection["attempts"])

            reached_replacement.release()
            replacement.wait()

    final = event_snapshot(factory, event_ids)
    final_ledger = read_ledger(ledger)
    attempts = final["attempts"]
    assert final["events"][0]["state"] == "applied"
    assert [(row["kind"], row["state"], row["worker_id"], row["terminal"]) for row in attempts] == [
        ("dispatch", "confirmed", replacement_worker_id, True)
    ]
    settlement = _authoritative_confirmed_settlement(final, attempt_id=attempts[0]["attempt_id"])
    assert final_ledger["dispatch_calls"] == 1
    assert final_ledger["recovery_observations"] == 0

    post_takeover_restart = start_projection_worker(
        dsn=dsn,
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id=replacement_worker_id,
        process_label="projection-section3-takeover-noop",
        identity_output=noop_identity,
        **identity_expectations,
    )
    post_takeover_restart.wait()
    after_noop = event_snapshot(factory, event_ids)
    after_noop_ledger = read_ledger(ledger)
    assert after_noop == final
    assert after_noop_ledger == final_ledger

    takeover_evidence = {
        "status": "passed",
        "original_worker_id": original_worker_id,
        "replacement_worker_id": replacement_worker_id,
        "original_claim": original_claim,
        "takeover_claim": takeover_claim,
        "final_settlement": final,
        "external_ledger": final_ledger,
        "settlement": settlement,
        "processes": {
            "original": _process_record(original),
            "replacement": _process_record(replacement),
            "post_settlement_restart": _process_record(post_takeover_restart),
        },
        "no_duplicate_after_takeover_restart": {
            "snapshot_unchanged": True,
            "external_ledger_unchanged": True,
            "snapshot": after_noop,
            "external_ledger": after_noop_ledger,
        },
    }
    stale_evidence = {
        "status": "passed",
        "original_worker_id": original_worker_id,
        "replacement_worker_id": replacement_worker_id,
        "original_pid": original.process.pid,
        "replacement_pid": replacement.process.pid,
        "original_log_recorded_stale_claim_rejection": True,
        "snapshot_before_original_resumed": takeover_claim,
        "snapshot_after_original_rejection": after_stale_rejection,
        "snapshot_unchanged": after_stale_rejection == takeover_claim,
        "original_worker_attempt_count": sum(
            row["worker_id"] == original_worker_id for row in after_stale_rejection["attempts"]
        ),
    }
    return takeover_evidence, stale_evidence, {
        "takeover_original": original_identity,
        "takeover_replacement": replacement_identity,
        "takeover_noop": noop_identity,
    }


def _exercise_downstream_failure(
    *,
    factory,
    ids,
    base_url: str,
    token: str,
    run_id: uuid.UUID,
    dsn: str,
    tmp_path: Path,
    identity_dir: Path,
    identity_expectations: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    created = _create_task(
        factory=factory,
        ids=ids,
        base_url=base_url,
        token=token,
        run_id=run_id,
        title="Runtime wiring downstream failure",
    )
    ledger = tmp_path / "downstream-failure-ledger.json"
    identity = identity_dir / "projection-downstream-identity.json"
    worker = start_projection_worker(
        dsn=dsn,
        tmp_path=tmp_path,
        ledger=ledger,
        worker_id="section3-downstream-failure",
        process_label="projection-section3-downstream-failure",
        scenario="downstream_failure",
        identity_output=identity,
        **identity_expectations,
    )
    worker.wait()
    snapshot = event_snapshot(factory, created["event_ids"])
    attempt = snapshot["attempts"][0]
    observation = snapshot["observations"][0]
    adjudication = snapshot["adjudications"][0]
    external = observation["evidence"]["external_observation"]
    assert snapshot["events"][0]["state"] == "pending"
    assert attempt["state"] == "not_applied" and attempt["terminal"] is True
    assert observation["kind"] == "marker_search"
    assert observation["observed_applied"] is False
    assert observation["reread_complete"] is True
    assert external["source"] == "external_marker_search"
    assert external["observed_absent"] is True
    assert adjudication["outcome"] == "not_applied"
    assert snapshot["correlations"][0]["state"] == "not_found"
    status, read_result = _command(
        base_url,
        token,
        command="read",
        run_id=run_id,
        request_id=None,
        arguments={"task_id": str(created["task_id"])},
    )
    assert status == 200 and read_result["ok"] is True
    freshness = read_result["data"]["projection_freshness"]
    assert freshness["fresh"] is False and freshness["state"] == "pending"
    return {
        "status": "passed",
        "process": _process_record(worker),
        "snapshot": snapshot,
        "external_ledger": read_ledger(ledger),
        "projection_freshness": freshness,
    }, identity


def run_runtime_wiring_rehearsal(core_db, tmp_path) -> dict[str, object]:
    factory, ids = core_db
    dsn = postgresql_dsn()
    database_name = str(make_url(dsn).database)
    with session_scope(factory) as session:
        schema_head = str(session.scalar(text("SELECT version_num FROM alembic_version")))
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=schema_head
        )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        epoch = ProjectionService(session, uuid_factory=lambda: next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="section 3 runtime wiring rehearsal",
            created_at=NOW,
            external_effects_enabled=True,
        )
        run_id = next(ids)
        WorkflowAuthorityService(session).register_run(
            run_id=run_id,
            generation_id=context["generation_id"],
            owner_id="cli",
            agent="codex",
            capability_digest=run_id.bytes + run_id.bytes,
            registered_at=NOW,
        )
        release = generation.dish_release
        generation_id = generation.generation_id

    private_port, action_port, proxy_port = _free_port(), _free_port(), _free_port()
    while len({private_port, action_port, proxy_port}) != 3:
        action_port, proxy_port = _free_port(), _free_port()
    forbidden_ports = {8765, 8766, 8775, 8776, 8786}
    assert {private_port, action_port, proxy_port}.isdisjoint(forbidden_ports)
    agent_token = "section3-agent-token-0001"
    admin_token = "section3-admin-token-0002"
    action_token = "section3-action-token-0003"
    current_proxy, service_dsn = start_postgresql_proxy(
        dsn=dsn,
        tmp_path=tmp_path,
        listen_port=proxy_port,
        label="postgresql-tcp-proxy-before-loss",
    )
    service = start_postgresql_service(
        dsn=service_dsn,
        tmp_path=tmp_path,
        expected_database=database_name,
        expected_schema_head=schema_head,
        expected_release=release,
        expected_generation_id=generation_id,
        private_port=private_port,
        action_port=action_port,
        agent_token=agent_token,
        admin_token=admin_token,
        action_token=action_token,
    )
    private_base_url = f"http://127.0.0.1:{private_port}"
    action_base_url = f"http://127.0.0.1:{action_port}"
    health_url = f"{private_base_url}/health"
    identity_dir = Path(os.environ.get("DISH_SECTION1_EVIDENCE_DIR", str(tmp_path))) / "runtime-identities"
    identity_dir.mkdir(parents=True, exist_ok=True)
    identity_expectations = {
        "expected_database": database_name,
        "expected_schema_head": schema_head,
        "expected_release": release,
        "expected_generation_id": generation_id,
    }
    evidence: dict[str, object] = {
        "database": database_name,
        "schema_head": schema_head,
        "release": release,
        "generation_id": str(generation_id),
        "projection_epoch_id": str(epoch.projection_epoch_id),
        "ports": {"private": private_port, "action": action_port, "postgresql_proxy": proxy_port},
    }
    try:
        health = _wait_health(health_url, expected_ok=True)
        assert health["profile"] == "test"
        assert health["identity"] == {
            "database": database_name,
            "schema_head": schema_head,
            "dish_release": release,
            "generation_id": str(generation_id),
            "generation_status": "active",
        }
        assert health["isolation"]["asana_environment_keys"] == []
        assert health["isolation"]["bind_host"] == "127.0.0.1"
        assert health["isolation"]["action_bind_host"] == "127.0.0.1"
        assert health["isolation"]["supported_http_surfaces"] == ["agent"]
        evidence["service_health"] = health
        evidence["unsupported_test_service_routes"] = _exercise_unsupported_routes(
            private_base_url=private_base_url,
            action_base_url=action_base_url,
            run_id=run_id,
            ids=ids,
            agent_token=agent_token,
            admin_token=admin_token,
            action_token=action_token,
        )

        same_restart, restart_identities = _exercise_same_worker_restart(
            factory=factory,
            ids=ids,
            base_url=private_base_url,
            token=agent_token,
            run_id=run_id,
            dsn=dsn,
            tmp_path=tmp_path,
            identity_dir=identity_dir,
            identity_expectations=identity_expectations,
        )
        takeover, stale_rejection, takeover_identities = _exercise_takeover_and_stale_rejection(
            factory=factory,
            ids=ids,
            base_url=private_base_url,
            token=agent_token,
            run_id=run_id,
            dsn=dsn,
            tmp_path=tmp_path,
            identity_dir=identity_dir,
            identity_expectations=identity_expectations,
        )
        evidence["same_logical_worker_restart"] = same_restart
        evidence["different_worker_takeover"] = takeover
        evidence["stale_original_worker_rejection"] = stale_rejection
        evidence["command_boundary"] = same_restart["command_boundary"]
        evidence["external_attempt_settlement"] = {
            "status": "passed",
            "same_logical_worker_restart": same_restart["after_same_logical_worker_restart"],
            "different_worker_takeover": {
                "snapshot": takeover["final_settlement"],
                "settlement": takeover["settlement"],
                "external_ledger": takeover["external_ledger"],
            },
            "no_duplicate_dispatch_or_settlement": {
                "after_same_worker_restart": same_restart[
                    "no_duplicate_after_post_settlement_restart"
                ],
                "after_takeover": takeover["no_duplicate_after_takeover_restart"],
            },
        }

        reconciliation_output = tmp_path / "reconciliation.json"
        reconciliation_identity = identity_dir / "reconciliation-identity.json"
        reconciliation = start_reconciliation_worker(
            dsn=dsn,
            tmp_path=tmp_path,
            ledger=tmp_path / "reconciliation-ledger.json",
            generation_id=generation_id,
            corpus_identity="section3-runtime-corpus",
            output=reconciliation_output,
            identity_output=reconciliation_identity,
            **{key: value for key, value in identity_expectations.items() if key != "expected_generation_id"},
        )
        reconciliation.wait()
        reconciliation_report = json.loads(reconciliation_output.read_text(encoding="utf-8"))
        assert reconciliation_report["ok"] is True
        assert reconciliation_report["generation_id"] == str(generation_id)
        assert reconciliation_report["projection_epoch_id"] == str(epoch.projection_epoch_id)
        evidence["reconciliation"] = reconciliation_report

        downstream, downstream_identity = _exercise_downstream_failure(
            factory=factory,
            ids=ids,
            base_url=private_base_url,
            token=agent_token,
            run_id=run_id,
            dsn=dsn,
            tmp_path=tmp_path,
            identity_dir=identity_dir,
            identity_expectations=identity_expectations,
        )
        evidence["downstream_failure_projection"] = downstream

        failed_request = next(ids)
        current_proxy.terminate()
        stopped_proxy = dict(current_proxy.manifest)
        down_health = _wait_health(health_url, expected_ok=False)
        failed_status, failed_closed = _command(
            private_base_url,
            agent_token,
            command="create",
            run_id=run_id,
            request_id=failed_request,
            arguments={"title": "Must not commit while PostgreSQL is down"},
        )
        assert failed_status >= 400
        assert failed_closed["ok"] is False
        assert failed_closed["code"] == "BACKEND_REJECTED"
        assert failed_closed["retryable"] is True
        current_proxy, restarted_service_dsn = start_postgresql_proxy(
            dsn=dsn,
            tmp_path=tmp_path,
            listen_port=proxy_port,
            label="postgresql-tcp-proxy-after-restart",
        )
        assert restarted_service_dsn == service_dsn
        recovered_health = _wait_health(health_url, expected_ok=True)
        with session_scope(factory) as session:
            assert session.get(wf.ServiceRequest, failed_request) is None
            leaked = int(
                session.scalar(
                    select(func.count())
                    .select_from(models.DishTask)
                    .where(models.DishTask.title == "Must not commit while PostgreSQL is down")
                )
                or 0
            )
        assert leaked == 0
        evidence["postgresql_loss"] = {
            "status": "passed",
            "stopped_proxy_process": stopped_proxy,
            "health_while_down": down_health,
            "mutation_status": failed_status,
            "mutation_result": failed_closed,
            "restarted_proxy_process": {
                "pid": current_proxy.process.pid,
                "command": current_proxy.manifest["command"],
                "completion_state_at_recovery": current_proxy.manifest["completion_state"],
            },
            "health_after_restart": recovered_health,
            "request_absent_after_restart": True,
            "task_absent_after_restart": True,
        }

        identity_paths = {
            **restart_identities,
            **takeover_identities,
            "reconciliation": reconciliation_identity,
            "downstream": downstream_identity,
        }
        identity_reports = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in identity_paths.items()
        }
        for report in identity_reports.values():
            assert report["ok"] is True
            assert report["identity"] == health["identity"]
        evidence["runtime_identities"] = identity_reports
        write_scenario("runtime-wiring-section3", evidence, nodeid=NODEID, tmp_path=tmp_path)
    finally:
        if service.process.poll() is None:
            service.terminate()
        if current_proxy.process.poll() is None:
            current_proxy.terminate()
    return evidence
