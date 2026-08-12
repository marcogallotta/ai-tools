from __future__ import annotations

from contextlib import nullcontext
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from dish_service.leases import ServicePrincipal
from dish_service.request_coordinators import AdminRequestCoordinator, AgentRequestCoordinator
from dish_service.shadow_capture import LegacyShadowCapture, ShadowCaptureSettings
from dish_service.path_safety import clear_kill_switch, engage_kill_switch
from dish_service.shadow_spool import ShadowSpool


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE task_content_state(task_gid TEXT PRIMARY KEY, value TEXT); INSERT INTO task_content_state VALUES ('t1','before');")
    conn.commit(); conn.close()


def test_request_surfaces_label_shadow_capture_principal_scope(monkeypatch):
    class CaptureProbe:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs["call"]()

    shadow = CaptureProbe()
    gate = SimpleNamespace(request=lambda: nullcontext())
    principal = ServicePrincipal(owner_id="owner", run_id="run")

    agent = AgentRequestCoordinator(
        SimpleNamespace(
            _maintenance_gate=gate,
            _planning_intent_execution_lock=lambda *_args: nullcontext(),
            _shadow_capture=shadow,
        ),
        initialization_error=lambda exc: exc,
    )
    monkeypatch.setattr(
        agent, "_execute_locked", lambda *_args, **_kwargs: {"ok": True}
    )
    assert agent.execute("start", {}, principal=principal, request_id="agent-request") == {
        "ok": True
    }
    assert shadow.calls[-1]["principal_class"] == "agent"

    admin = AdminRequestCoordinator(
        SimpleNamespace(_maintenance_gate=gate, _shadow_capture=shadow),
        initialization_error=lambda exc: exc,
    )
    monkeypatch.setattr(
        admin, "_execute_locked", lambda *_args, **_kwargs: {"ok": True}
    )
    assert admin.execute(
        "supply-evidence", {}, principal=principal, request_id="admin-request"
    ) == {"ok": True}
    assert shadow.calls[-1]["principal_class"] == "admin"


def test_capture_persists_explicit_principal_class_without_changing_live_result(tmp_path):
    db = tmp_path / "live.sqlite3"
    _db(db)
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"
        ),
        db_path=db,
    )
    principal = ServicePrincipal(owner_id="owner", run_id="run")
    for request_id, principal_class in (
        ("agent-capture", "agent"),
        ("admin-capture", "admin"),
    ):
        expected = {"ok": True, "data": {"surface": principal_class}}
        result = capture.execute(
            command="start",
            arguments={"task_gid": "t1"},
            principal=principal,
            principal_class=principal_class,
            request_id=request_id,
            call=lambda expected=expected: expected,
        )
        assert result is expected
        item = capture.spool.get_by_source_identity(request_id)
        assert item is not None
        assert item.principal == {
            "owner_id": "owner",
            "run_id": "run",
            "principal_class": principal_class,
        }


def test_capture_mirrors_completion_without_changing_result(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    capture = LegacyShadowCapture(
        ShadowCaptureSettings("capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"),
        db_path=db,
    )
    def call():
        conn=sqlite3.connect(db); conn.execute("UPDATE task_content_state SET value='after' WHERE task_gid='t1'"); conn.commit(); conn.close()
        return {"ok": True, "data": {"task_gid": "t1"}}
    result = capture.execute(command="start", arguments={"task_gid":"t1"}, principal=None, request_id="r1", call=call)
    assert result["ok"] is True
    item = capture.spool.get_by_source_identity("r1")
    assert item is not None and item.state == "complete"
    assert item.source_pre_state["tables"]["task_content_state"][0]["value"] == "before"
    assert item.source_post_state["tables"]["task_content_state"][0]["value"] == "after"


def test_capture_uses_generated_result_identity_to_bound_post_state(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    capture = LegacyShadowCapture(
        ShadowCaptureSettings("capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"),
        db_path=db,
    )
    def call():
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO task_content_state VALUES ('created-task','created')")
        conn.commit(); conn.close()
        return {"ok": True, "task_gid": "created-task", "data": {}}

    capture.execute(
        command="create", arguments={"title": "Created"}, principal=None,
        request_id="generated-task", call=call,
    )
    item = capture.spool.get_by_source_identity("generated-task")
    assert item is not None
    assert item.source_pre_state["task_gids"] == []
    assert item.source_post_state["task_gids"] == ["created-task"]
    assert item.source_post_state["tables"]["task_content_state"][0]["value"] == "created"


def test_capture_failure_never_replaces_live_result(tmp_path, monkeypatch):
    db = tmp_path / "live.sqlite3"; _db(db)
    capture = LegacyShadowCapture(
        ShadowCaptureSettings("capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"),
        db_path=db,
    )
    monkeypatch.setattr(capture.spool, "reserve", lambda **_: (_ for _ in ()).throw(OSError("disk full")))
    expected = {"ok": True, "data": {"unchanged": True}}
    assert capture.execute(command="start", arguments={"task_gid":"t1"}, principal=None, request_id="r2", call=lambda: expected) is expected
    assert list((tmp_path / "emergency").glob("*.json"))


def test_snapshot_schema_error_fails_capture_open_instead_of_recording_empty_evidence(tmp_path):
    db = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE operations(operation_id TEXT PRIMARY KEY)")
    connection.commit(); connection.close()
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"
        ),
        db_path=db,
    )
    expected = {"ok": True}
    assert capture.execute(
        command="start", arguments={"task_gid": "t1"}, principal=None,
        request_id="bad-snapshot-schema", call=lambda: expected,
    ) is expected
    assert capture.spool.get_by_source_identity("bad-snapshot-schema") is None
    assert list((tmp_path / "emergency").glob("*.json"))


def test_startup_spool_failure_disables_capture_for_the_process(tmp_path, monkeypatch):
    db = tmp_path / "live.sqlite3"; _db(db)
    monkeypatch.setattr(
        ShadowSpool,
        "initialize",
        lambda _self: (_ for _ in ()).throw(OSError("spool unavailable")),
    )
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"
        ),
        db_path=db,
    )
    monkeypatch.setattr(
        capture.spool,
        "reserve",
        lambda **_: (_ for _ in ()).throw(AssertionError("capture retried on live path")),
    )
    expected = {"ok": True, "data": {"live": True}}
    assert capture.execute(
        command="start",
        arguments={"task_gid": "t1"},
        principal=None,
        request_id="startup-failure",
        call=lambda: expected,
    ) is expected


def test_kill_switch_bypasses_capture(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    switch=tmp_path/"disabled"; switch.write_text("disabled")
    capture = LegacyShadowCapture(
        ShadowCaptureSettings("capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1", switch),
        db_path=db,
    )
    result=capture.execute(command="start", arguments={"task_gid":"t1"}, principal=None, request_id="r3", call=lambda:{"ok":True})
    assert result == {"ok": True}
    assert capture.spool.status()["counts"]["complete"] == 0


def test_capture_can_be_reenabled_without_initializing_spool_on_live_request(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    switch = tmp_path / "disabled"
    engage_kill_switch(switch, {"disabled": True, "reason": "startup disabled"})
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture", tmp_path / "spool.sqlite3", tmp_path / "emergency",
            "legacy-1", switch,
        ),
        db_path=db,
    )
    clear_kill_switch(switch)
    capture.execute(
        command="start", arguments={"task_gid": "t1"}, principal=None,
        request_id="reenabled", call=lambda: {"ok": True},
    )
    assert capture.spool.get_by_source_identity("reenabled").state == "complete"


def test_unknown_command_and_legacy_exception_preserve_live_semantics(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    capture = LegacyShadowCapture(
        ShadowCaptureSettings("capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1"),
        db_path=db,
    )
    assert capture.execute(command="future-command", arguments={}, principal=None, request_id="r4", call=lambda:{"ok":True}) == {"ok":True}
    try:
        capture.execute(command="start", arguments={"task_gid":"t1"}, principal=None, request_id="r5", call=lambda:(_ for _ in ()).throw(RuntimeError("live failure")))
    except RuntimeError as exc:
        assert str(exc) == "live failure"
    else:
        raise AssertionError("live exception was swallowed")
    assert capture.spool.get_by_source_identity("r5").state == "gap"


def test_locked_spool_fails_open_without_live_path_stall(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture",
            tmp_path / "spool.sqlite3",
            tmp_path / "emergency",
            "legacy-1",
            busy_timeout_ms=25,
        ),
        db_path=db,
    )
    blocker = sqlite3.connect(capture.spool.path, isolation_level=None)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        expected = {"ok": True, "data": {"live": True}}
        started = time.monotonic()
        result = capture.execute(
            command="start",
            arguments={"task_gid": "t1"},
            principal=None,
            request_id="locked-spool",
            call=lambda: expected,
        )
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()
    assert result is expected
    assert elapsed < 0.75
    assert list((tmp_path / "emergency").glob("*.json"))


def test_capacity_guard_engages_kill_switch_without_changing_live_result(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    switch = tmp_path / "dark-launch.disabled"
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture",
            tmp_path / "spool.sqlite3",
            tmp_path / "emergency",
            "legacy-1",
            switch,
            max_spool_records=1,
            min_free_bytes=1,
        ),
        db_path=db,
    )
    capture.execute(
        command="start",
        arguments={"task_gid": "t1"},
        principal=None,
        request_id="capacity-1",
        call=lambda: {"ok": True},
    )
    expected = {"ok": True, "data": {"live": "unchanged"}}
    result = capture.execute(
        command="start",
        arguments={"task_gid": "t1"},
        principal=None,
        request_id="capacity-2",
        call=lambda: expected,
    )
    assert result is expected
    assert switch.exists()
    assert "capacity guard" in switch.read_text()
    assert list((tmp_path / "emergency").glob("*.json"))


def test_capture_rejects_authority_spool_or_kill_switch_alias(tmp_path):
    import os
    import pytest

    from dish_service.path_safety import PathIdentityError

    db = tmp_path / "live.sqlite3"
    _db(db)
    hardlink = tmp_path / "same-live.sqlite3"
    os.link(db, hardlink)

    with pytest.raises(PathIdentityError):
        LegacyShadowCapture(
            ShadowCaptureSettings(
                "capture", hardlink, tmp_path / "emergency", "legacy-1"
            ),
            db_path=db,
        )
    with pytest.raises(PathIdentityError):
        LegacyShadowCapture(
            ShadowCaptureSettings(
                "capture",
                tmp_path / "spool.sqlite3",
                tmp_path / "emergency",
                "legacy-1",
                db,
            ),
            db_path=db,
        )


def test_snapshot_captures_all_operation_linked_service_requests_for_creator_proof(tmp_path):
    from dish_service.shadow_capture import authoritative_snapshot

    db = tmp_path / "lineage.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            owner_id TEXT,
            run_id TEXT,
            command TEXT NOT NULL,
            request_hash TEXT,
            status TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT,
            result_json TEXT,
            resolution_result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        """
    )
    operation_id = "operation-1"
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?)",
        (operation_id, "task-1", "initial", "2026-08-09T08:00:00+00:00"),
    )
    creator_result = (
        '{"ok":true,"command":"start","task_gid":"task-1",'
        '"submission_id":"operation-1","data":{"operation_id":"operation-1",'
        '"operation_kind":"initial"}}'
    )
    conn.execute(
        "INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "creator-request", "owner", "run", "start", "hash-a", "completed",
            operation_id, "task-1", creator_result, None,
            "2026-08-09T07:59:59+00:00", "2026-08-09T08:00:01+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "later-request", "owner", "run", "start", "hash-b", "completed",
            operation_id, "task-1", "{}", "{}",
            "2026-08-09T08:01:00+00:00", "2026-08-09T08:01:01+00:00",
        ),
    )
    conn.commit()
    conn.close()

    snapshot = authoritative_snapshot(
        db,
        arguments={"submission_id": operation_id},
        request_id=None,
    )

    requests = snapshot["tables"]["service_requests"]
    assert {row["request_id"] for row in requests} == {
        "creator-request", "later-request"
    }
    assert snapshot["lineage_scope"]["operation_ids"] == [operation_id]
    assert snapshot["lineage_scope"]["explicit_request_ids"] == []


def test_snapshot_expands_operation_succession_lineage_for_operation_resolution(tmp_path):
    from dish_service.shadow_capture import authoritative_snapshot

    db = tmp_path / "succession.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            owner_id TEXT,
            run_id TEXT,
            command TEXT NOT NULL,
            request_hash TEXT,
            status TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT,
            result_json TEXT,
            resolution_result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE operation_successions(
            succession_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            source_operation_id TEXT NOT NULL,
            successor_operation_id TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO operations VALUES (?,?,?,?)",
        [
            ("source-op", "task-1", "initial", "2026-08-09T08:00:00+00:00"),
            ("successor-op", "task-1", "initial", "2026-08-09T08:02:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "creator-request", "owner", "run", "start", "hash", "completed",
            "source-op", "task-1", "{}", None,
            "2026-08-09T07:59:59+00:00", "2026-08-09T08:00:01+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO operation_successions VALUES (?,?,?,?)",
        ("succession-1", "task-1", "source-op", "successor-op"),
    )
    conn.commit()
    conn.close()

    snapshot = authoritative_snapshot(
        db,
        arguments={"target_operation_id": "successor-op"},
        request_id=None,
    )

    assert snapshot["lineage_scope"]["operation_ids"] == ["source-op", "successor-op"]
    assert snapshot["lineage_scope"]["explicit_request_ids"] == []
    assert {
        row["operation_id"] for row in snapshot["tables"]["operations"]
    } == {"source-op", "successor-op"}
    assert snapshot["tables"]["operation_successions"][0]["succession_id"] == "succession-1"
    assert snapshot["tables"]["service_requests"][0]["request_id"] == "creator-request"


def test_completion_capacity_guard_kills_capture_and_retains_only_gap(tmp_path):
    db = tmp_path / "live.sqlite3"
    _db(db)
    switch = tmp_path / "dark-launch.disabled"
    capture = LegacyShadowCapture(
        ShadowCaptureSettings(
            "capture",
            tmp_path / "spool.sqlite3",
            tmp_path / "emergency",
            "legacy-1",
            switch,
            max_spool_bytes=256 * 1024,
            min_free_bytes=1,
        ),
        db_path=db,
    )
    result = {"ok": True, "payload": "x" * (512 * 1024)}

    assert capture.execute(
        command="start",
        arguments={"task_gid": "t1"},
        principal=None,
        request_id="large-completion",
        call=lambda: result,
    ) is result

    assert switch.exists()
    item = capture.spool.get_by_source_identity("large-completion")
    assert item is not None
    assert item.state in {"reserved", "gap"}
    assert item.source_outcome is None



def test_patch_b_snapshot_expands_verification_continuation_cycle_lineage(tmp_path):
    from dish_service.shadow_capture import authoritative_snapshot

    db = tmp_path / "continuation-cycle.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            owner_id TEXT,
            run_id TEXT,
            command TEXT NOT NULL,
            request_hash TEXT,
            status TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT,
            result_json TEXT,
            resolution_result_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE verification_cycles(
            cycle_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            task_gid TEXT NOT NULL,
            cycle_number INTEGER NOT NULL
        );
        CREATE TABLE operation_successions(
            succession_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            source_operation_id TEXT NOT NULL,
            successor_operation_id TEXT NOT NULL,
            source_cycle_id TEXT,
            successor_cycle_id TEXT,
            abandonment_id TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO operations VALUES (?,?,?,?)",
        [
            ("source-op", "task-1", "initial", "2026-08-09T08:00:00+00:00"),
            ("successor-op", "task-1", "initial", "2026-08-09T08:02:00+00:00"),
        ],
    )
    conn.execute(
        "INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "creator-request", "owner", "run", "start", "hash", "completed",
            "source-op", "task-1", "{}", None,
            "2026-08-09T07:59:59+00:00", "2026-08-09T08:00:01+00:00",
        ),
    )
    conn.executemany(
        "INSERT INTO verification_cycles VALUES (?,?,?,?)",
        [
            ("source-cycle", "source-op", "task-1", 1),
            ("successor-cycle", "successor-op", "task-1", 2),
        ],
    )
    conn.execute(
        "INSERT INTO operation_successions VALUES (?,?,?,?,?,?,?)",
        (
            "succession-1", "task-1", "source-op", "successor-op",
            "source-cycle", "successor-cycle", None,
        ),
    )
    conn.commit()
    conn.close()

    snapshot = authoritative_snapshot(
        db,
        arguments={
            "target_operation_id": "successor-op",
            "target_cycle_id": "successor-cycle",
        },
        request_id=None,
    )

    assert snapshot["lineage_scope"]["operation_ids"] == ["source-op", "successor-op"]
    assert snapshot["lineage_scope"]["cycle_ids"] == ["source-cycle", "successor-cycle"]
    assert {
        row["cycle_id"] for row in snapshot["tables"]["verification_cycles"]
    } == {"source-cycle", "successor-cycle"}


def test_patch_b_snapshot_captures_direct_actor_lease_lineage(tmp_path):
    from dish_service.shadow_capture import authoritative_snapshot

    db = tmp_path / "lease-lineage.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE service_leases(
            lease_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            task_gid TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            lease_kind TEXT,
            actor_attempt_seq INTEGER,
            context_cycle_id TEXT
        );
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?)",
        ("operation-1", "task-1", "initial", "2026-08-09T08:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO service_leases VALUES (?,?,?,?,?,?,?,?)",
        ("lease-1", "operation-1", "task-1", "owner", "run", "actor", 3, None),
    )
    conn.commit()
    conn.close()

    snapshot = authoritative_snapshot(
        db,
        arguments={"lease_id": "lease-1"},
        request_id=None,
    )

    assert snapshot["lineage_scope"]["lease_ids"] == ["lease-1"]
    assert snapshot["lineage_scope"]["operation_ids"] == ["operation-1"]
    assert snapshot["tables"]["service_leases"][0]["lease_id"] == "lease-1"


def test_patch_b_snapshot_captures_planning_challenge_creator_request_lineage(tmp_path):
    from dish_service.shadow_capture import authoritative_snapshot

    db = tmp_path / "planning-challenge.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE planning_intent_challenges(
            challenge_id TEXT PRIMARY KEY,
            created_request_id TEXT NOT NULL,
            claimed_request_id TEXT,
            owner_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            task_gid TEXT NOT NULL,
            agent TEXT NOT NULL,
            target_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            operation_id TEXT
        );
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO service_requests VALUES (?,?,?,?,?)",
        ("issue-request", "start", "completed", None, "task-1"),
    )
    conn.execute(
        "INSERT INTO planning_intent_challenges VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "challenge-1", "issue-request", None, "owner", "run", "task-1",
            "claude", "target-hash", "issued", None,
        ),
    )
    conn.commit()
    conn.close()

    snapshot = authoritative_snapshot(
        db,
        arguments={"intent_challenge_id": "challenge-1"},
        request_id=None,
    )

    assert snapshot["lineage_scope"]["challenge_ids"] == ["challenge-1"]
    assert snapshot["lineage_scope"]["explicit_request_ids"] == ["issue-request"]
    assert snapshot["tables"]["planning_intent_challenges"][0]["challenge_id"] == "challenge-1"
    assert snapshot["tables"]["service_requests"][0]["request_id"] == "issue-request"


def test_patch_b_snapshot_captures_abandonment_creation_request_lineage(tmp_path):
    from dish_service.shadow_capture import authoritative_snapshot

    db = tmp_path / "abandonment-lineage.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE service_leases(
            lease_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            task_gid TEXT NOT NULL,
            context_cycle_id TEXT
        );
        CREATE TABLE abandonment_attempts(
            abandonment_id TEXT PRIMARY KEY,
            task_gid TEXT NOT NULL,
            source_operation_id TEXT NOT NULL,
            source_lease_id TEXT NOT NULL,
            successor_operation_id TEXT,
            continuation_operation_id TEXT,
            attempt_cycle_id TEXT,
            successor_cycle_id TEXT,
            continuation_cycle_id TEXT
        );
        CREATE TABLE operation_executions(
            execution_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            request_id TEXT,
            command TEXT NOT NULL
        );
        CREATE TABLE audit_events(
            event_id TEXT PRIMARY KEY,
            operation_id TEXT,
            event_type TEXT NOT NULL,
            operation_execution_id TEXT,
            details TEXT NOT NULL
        );
        CREATE TABLE service_requests(
            request_id TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            status TEXT NOT NULL,
            operation_id TEXT,
            task_gid TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?)",
        ("operation-1", "task-1", "initial", "2026-08-09T08:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO service_leases VALUES (?,?,?,?)",
        ("lease-1", "operation-1", "task-1", None),
    )
    conn.execute(
        "INSERT INTO service_requests VALUES (?,?,?,?,?)",
        ("abandon-request", "abandon-operation", "completed", "operation-1", "task-1"),
    )
    conn.execute(
        "INSERT INTO operation_executions VALUES (?,?,?,?)",
        ("abandon-execution", "operation-1", "abandon-request", "abandon-operation"),
    )
    conn.execute(
        "INSERT INTO audit_events VALUES (?,?,?,?,?)",
        (
            "audit-1", "operation-1", "operation.abandonment_started",
            "abandon-execution",
            '{"abandonment_id":"abandonment-1","source_lease_id":"lease-1"}',
        ),
    )
    conn.execute(
        "INSERT INTO abandonment_attempts VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "abandonment-1", "task-1", "operation-1", "lease-1", None, None,
            None, None, None,
        ),
    )
    conn.commit()
    conn.close()

    snapshot = authoritative_snapshot(
        db,
        arguments={"abandonment_id": "abandonment-1"},
        request_id=None,
    )

    assert snapshot["lineage_scope"]["abandonment_ids"] == ["abandonment-1"]
    assert snapshot["lineage_scope"]["lease_ids"] == ["lease-1"]
    assert snapshot["lineage_scope"]["operation_ids"] == ["operation-1"]
    assert snapshot["lineage_scope"]["explicit_request_ids"] == ["abandon-request"]
    assert snapshot["tables"]["operation_executions"][0]["request_id"] == "abandon-request"
    assert snapshot["tables"]["service_requests"][0]["request_id"] == "abandon-request"
