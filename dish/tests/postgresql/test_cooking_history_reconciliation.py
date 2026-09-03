from __future__ import annotations
import sqlite3
from dish_pg.cooking_history_reconciliation import export_history_isolated
from dish_pg.importer import iter_source


def test_departed_history_task_exports_as_isolated(tmp_path):
    db = tmp_path / "legacy.db"; conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE task_content_state(task_gid TEXT PRIMARY KEY,last_confirmed_identity TEXT,last_confirmed_title TEXT,last_confirmed_notes TEXT,schema_version TEXT,confirmed_at TEXT); CREATE TABLE operations(operation_id TEXT,task_gid TEXT,operation_kind TEXT,status TEXT,phase TEXT,terminal_outcome TEXT,created_at TEXT,completed_at TEXT); CREATE TABLE service_leases(lease_id TEXT,operation_id TEXT,task_gid TEXT,owner_id TEXT,run_id TEXT,lease_kind TEXT,actor_attempt_seq INTEGER,context_cycle_id TEXT,acquired_at TEXT,expires_at TEXT,released_at TEXT); CREATE TABLE verification_cycles(cycle_id TEXT,operation_id TEXT,task_gid TEXT,cycle_number INTEGER,outcome TEXT,created_at TEXT,completed_at TEXT); CREATE TABLE operation_run_revocations(revocation_id TEXT,operation_id TEXT,owner_id TEXT,run_id TEXT,source_lease_id TEXT,reason TEXT,revoked_at TEXT);")
    conn.execute("INSERT INTO task_content_state VALUES (?,?,?,?,?,?)", ("123","id","Title","Body","v1","2026-09-03T00:00:00+00:00")); conn.commit(); conn.close()
    manifest = tmp_path / "locations.json"; manifest.write_text('{"tasks":{}}')
    snapshot = tmp_path / "history.json"; snapshot.write_text('{"tasks":[{"gid":"123","completed":true}]}')
    out = tmp_path / "source.ndjson"; assert export_history_isolated(database=db, location_manifest=manifest, history_snapshot=snapshot, output=out) == 1
    spec = next(iter_source(out)).spec
    assert spec is not None and spec.existence_state == "isolated" and spec.completed and not spec.project_ids and spec.section_id is None
