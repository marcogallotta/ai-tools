from __future__ import annotations

import sqlite3
import uuid

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.database_initialization import initialize_database
from dish_tool.database_schema import MIGRATIONS, _execute_script_statements
from tests.support.service_leases import _service
from tests.support.operational import Clock
from tests.support.verification import Backend, TASK


def _principal(owner: str, run: str) -> ServicePrincipal:
    return ServicePrincipal(owner_id=owner, run_id=run)


def test_actor_attempt_sequence_and_verification_cycle_context_are_durable(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    researcher = _principal("researcher", "research-run")

    started = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "initial",
            "run_id": researcher.run_id,
        },
        principal=researcher,
        request_id=str(uuid.uuid4()),
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
        principal=researcher,
        request_id=str(uuid.uuid4()),
    )
    assert prepared["ok"]

    verifier = _principal("verifier", "verify-run")
    reviewed = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "run_id": verifier.run_id,
            "independence_attestation": "independent",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert reviewed["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        cycle = conn.execute(
            "SELECT cycle_id FROM verification_cycles WHERE operation_id=?",
            (started["submission_id"],),
        ).fetchone()
        leases = conn.execute(
            """SELECT lease_kind,actor_attempt_seq,context_cycle_id,owner_id,run_id
                 FROM service_leases WHERE task_gid='t'
                 ORDER BY actor_attempt_seq"""
        ).fetchall()
        assert [row["lease_kind"] for row in leases] == ["actor", "actor"]
        assert [row["actor_attempt_seq"] for row in leases] == [1, 2]
        assert leases[0]["context_cycle_id"] is None
        assert leases[1]["context_cycle_id"] == cycle["cycle_id"]
        assert (leases[1]["owner_id"], leases[1]["run_id"]) == (
            verifier.owner_id,
            verifier.run_id,
        )

        with pytest.raises(sqlite3.IntegrityError, match="attempt context is immutable"):
            conn.execute(
                "UPDATE service_leases SET actor_attempt_seq=99 WHERE actor_attempt_seq=2"
            )
    finally:
        conn.close()


@pytest.mark.parametrize("route", ["evidence", "human-review"])
def test_preconstruction_reject_reclaims_same_stage_actor_without_cycle_context(
    tmp_path, route,
):
    backend = Backend()
    clock = Clock()
    service = _service(tmp_path, backend, clock=clock, ttl=60)
    researcher = _principal("researcher", "research-run")

    started = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "initial",
            "run_id": researcher.run_id,
        },
        principal=researcher,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"]
    operation_id = started["submission_id"]

    clock.advance(61)
    recovered = service.recover_lease(
        operation_id,
        _principal("marco", "admin-run"),
        reason="test expired actor recovery",
    )
    assert recovered["ok"]

    fresh_run = _principal("researcher", "fresh-run")
    forbidden = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": operation_id,
            "route": route,
            "reason": "Need authoritative source before construction",
            "resume_status": "pending-research",
            **({
                "human_review_confirmed": True,
                "human_review_basis": "The remaining pre-construction choice requires Marco's authority.",
                "repairs_considered": "Within-authority research routes were considered and cannot settle that choice.",
            } if route == "human-review" else {}),
        },
        principal=fresh_run,
        request_id=str(uuid.uuid4()),
    )
    assert not forbidden["ok"]
    assert forbidden["code"] == "AGENT_MISMATCH"
    assert forbidden["errors"][0]["rule"] == "service_lease_claim_forbidden"

    rejected = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": operation_id,
            "route": route,
            "reason": "Need authoritative source before construction",
            "resume_status": "pending-research",
            **({
                "human_review_confirmed": True,
                "human_review_basis": "The remaining pre-construction choice requires Marco's authority.",
                "repairs_considered": "Within-authority research routes were considered and cannot settle that choice.",
            } if route == "human-review" else {}),
        },
        principal=researcher,
        request_id=str(uuid.uuid4()),
    )
    assert rejected["ok"], rejected

    conn = initialize_database(service.config.db_path)
    try:
        leases = conn.execute(
            """SELECT lease_kind,actor_attempt_seq,context_cycle_id,released_at
                 FROM service_leases WHERE operation_id=?
                 ORDER BY actor_attempt_seq""",
            (operation_id,),
        ).fetchall()
        assert len(leases) == 2
        assert [row["lease_kind"] for row in leases] == ["actor", "actor"]
        assert [row["actor_attempt_seq"] for row in leases] == [1, 2]
        assert [row["context_cycle_id"] for row in leases] == [None, None]
        assert leases[1]["released_at"] is not None
    finally:
        conn.close()


def test_operation_admin_lease_is_classified_without_consuming_actor_sequence(tmp_path):
    backend = Backend()
    service = _service(tmp_path, backend)
    actor = _principal("researcher", "research-run")
    started = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "t",
            "kind": "initial",
            "run_id": actor.run_id,
        },
        principal=actor,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        LeaseManager(conn).release(
            started["submission_id"], None, reason="test actor unavailable", admin=True
        )
    finally:
        conn.close()

    result = service.execute_admin(
        "discard",
        {"submission_id": started["submission_id"], "reason": "test cleanup"},
        principal=_principal("marco", "admin-run"),
        request_id=str(uuid.uuid4()),
    )
    assert result["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        rows = conn.execute(
            """SELECT lease_kind,actor_attempt_seq,context_cycle_id,released_at
                 FROM service_leases WHERE task_gid='t' ORDER BY acquired_at"""
        ).fetchall()
        assert len(rows) == 2
        assert tuple(rows[0][:3]) == ("actor", 1, None)
        assert tuple(rows[1][:3]) == ("admin_request", None, None)
        assert rows[1]["released_at"] is not None
    finally:
        conn.close()


def test_migration_preserves_legacy_lease_as_unclassified(tmp_path):
    db_path = tmp_path / "legacy-v30.sqlite"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 31):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version = {version}")
        conn.execute(
            """INSERT INTO operations(
                   operation_id,task_gid,operation_kind,status,phase,
                   expected_identity,expected_section_gid,schema_version,created_at
               ) VALUES('op','task','initial','open','prepare_required',
                        'identity','section','2','2026-07-30T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO service_leases(
                   lease_id,operation_id,task_gid,owner_id,run_id,
                   acquired_at,renewed_at,expires_at
               ) VALUES('legacy','op','task','owner','old-run',
                        '2026-07-30T00:00:00Z','2026-07-30T00:00:00Z',
                        '2026-07-30T01:00:00Z')"""
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    conn = initialize_database(db_path)
    try:
        legacy = conn.execute(
            "SELECT lease_kind,actor_attempt_seq,context_cycle_id FROM service_leases WHERE lease_id='legacy'"
        ).fetchone()
        assert tuple(legacy) == (None, None, None)

        manager = LeaseManager(conn)
        manager.release("op", None, reason="legacy drained", admin=True)
        replacement = manager.acquire("op", _principal("owner", "new-run"))
        assert replacement["lease_kind"] == "actor"
        assert replacement["actor_attempt_seq"] == 1
        assert replacement["context_cycle_id"] is None
    finally:
        conn.close()
