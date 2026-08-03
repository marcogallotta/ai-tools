from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from dish_service.shadow_capture import LegacyShadowCapture, ShadowCaptureSettings
from dish_service.path_safety import clear_kill_switch, engage_kill_switch
from dish_service.shadow_spool import ShadowSpool


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE task_content_state(task_gid TEXT PRIMARY KEY, value TEXT); INSERT INTO task_content_state VALUES ('t1','before');")
    conn.commit(); conn.close()


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
