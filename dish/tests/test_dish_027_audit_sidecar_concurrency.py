from __future__ import annotations

import json
import threading
import time

import dish_tool.database as database_module
from dish_tool.database import _import_command_audit_repair_fallback
from dish_tool.database_schema import initialize_database
from dish_tool.invocation_audit import _write_emergency_repair


def _repair(repair_id: str) -> dict:
    return {
        "repair_id": repair_id,
        "command": "dish.read",
        "operation_id": None,
        "submission_id": None,
        "task_gid": "task-1",
        "actor_agent": None,
        "result": {
            "ok": True,
            "code": "OK",
            "state": "read",
            "retryable": False,
        },
        "audit_error": "simulated audit outage",
    }


def _repair_ids(conn) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT repair_id FROM command_audit_repairs ORDER BY repair_id"
        ).fetchall()
    ]


def test_import_does_not_delete_concurrent_emergency_append(monkeypatch, tmp_path):
    db_path = tmp_path / "dish.db"
    setup = initialize_database(db_path)
    assert _write_emergency_repair(setup, _repair("repair-before-import"))
    setup.close()

    importer_inside_claim = threading.Event()
    release_importer = threading.Event()
    writer_finished = threading.Event()
    real_loads = json.loads
    first_load = True

    def blocking_loads(value, *args, **kwargs):
        nonlocal first_load
        if first_load:
            first_load = False
            importer_inside_claim.set()
            assert release_importer.wait(timeout=5)
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(database_module.json, "loads", blocking_loads)
    imported_counts: list[int] = []

    def importer():
        conn = initialize_database(db_path)
        try:
            imported_counts.append(_import_command_audit_repair_fallback(conn))
        finally:
            conn.close()

    def writer():
        conn = initialize_database(db_path)
        try:
            assert _write_emergency_repair(conn, _repair("repair-concurrent"))
        finally:
            conn.close()
            writer_finished.set()

    importer_thread = threading.Thread(target=importer)
    importer_thread.start()
    assert importer_inside_claim.wait(timeout=5)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    time.sleep(0.1)
    assert not writer_finished.is_set(), "writer must wait for the importer sidecar lock"

    release_importer.set()
    importer_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    assert not importer_thread.is_alive()
    assert not writer_thread.is_alive()
    assert imported_counts == [1]

    sidecar = tmp_path / "dish.db.audit-repair.jsonl"
    remaining = [json.loads(line) for line in sidecar.read_text().splitlines()]
    assert [item["repair_id"] for item in remaining] == ["repair-concurrent"]

    check = initialize_database(db_path)
    try:
        assert _repair_ids(check) == ["repair-before-import"]
        assert _import_command_audit_repair_fallback(check) == 1
        assert _repair_ids(check) == ["repair-before-import", "repair-concurrent"]
        assert not sidecar.exists()
    finally:
        check.close()


def test_import_recovers_stale_claim_before_current_sidecar(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    conn.close()

    sidecar = tmp_path / "dish.db.audit-repair.jsonl"
    claim = tmp_path / "dish.db.audit-repair.jsonl.importing"
    claim.write_text(json.dumps(_repair("repair-stale-claim")) + "\n")
    sidecar.write_text(json.dumps(_repair("repair-current")) + "\n")

    check = initialize_database(db_path)
    try:
        assert _import_command_audit_repair_fallback(check) == 2
        assert _repair_ids(check) == ["repair-current", "repair-stale-claim"]
        assert not claim.exists()
        assert not sidecar.exists()
    finally:
        check.close()
