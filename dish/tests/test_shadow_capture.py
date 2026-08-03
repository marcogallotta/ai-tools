from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from dish_service.shadow_capture import LegacyShadowCapture, ShadowCaptureSettings


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


def test_kill_switch_bypasses_capture(tmp_path):
    db = tmp_path / "live.sqlite3"; _db(db)
    switch=tmp_path/"disabled"; switch.write_text("disabled")
    capture = LegacyShadowCapture(
        ShadowCaptureSettings("capture", tmp_path / "spool.sqlite3", tmp_path / "emergency", "legacy-1", switch),
        db_path=db,
    )
    result=capture.execute(command="start", arguments={"task_gid":"t1"}, principal=None, request_id="r3", call=lambda:{"ok":True})
    assert result == {"ok": True}
    assert not (tmp_path/"spool.sqlite3").exists()


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
