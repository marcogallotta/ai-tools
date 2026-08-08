from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from dish_pg.importer import iter_source
from dish_pg.legacy_source import LegacySourceError, export_legacy_source


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE task_content_state(
            task_gid TEXT PRIMARY KEY,last_confirmed_identity TEXT,last_confirmed_title TEXT,
            last_confirmed_notes TEXT,schema_version TEXT,confirmed_at TEXT);
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,task_gid TEXT,operation_kind TEXT,status TEXT,
            created_at TEXT,completed_at TEXT,phase TEXT,terminal_outcome TEXT);
        CREATE TABLE verification_cycles(
            cycle_id TEXT PRIMARY KEY,operation_id TEXT,task_gid TEXT,cycle_number INTEGER,
            outcome TEXT,created_at TEXT,completed_at TEXT);
        CREATE TABLE service_leases(
            lease_id TEXT PRIMARY KEY,operation_id TEXT,task_gid TEXT,owner_id TEXT,run_id TEXT,
            acquired_at TEXT,expires_at TEXT,released_at TEXT,lease_kind TEXT,
            actor_attempt_seq INTEGER,context_cycle_id TEXT);
        """
    )
    conn.execute(
        "INSERT INTO task_content_state VALUES (?,?,?,?,?,?)",
        ("123", "id-1", "Title", "Body", "schema-1", "2026-08-03T09:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def _manifest(path, *, task_id, project_id, section_id):
    path.write_text(
        json.dumps(
            {
                "tasks": {
                    "123": {
                        "task_id": str(task_id),
                        "project_ids": [str(project_id)],
                        "section_id": str(section_id),
                        "section_gid": "901",
                        "completed": False,
                        "observed_at": "2026-08-03T09:01:00+00:00",
                    }
                }
            }
        )
    )


def test_legacy_source_is_deterministic_and_importer_compatible(tmp_path):
    db=tmp_path/"live.sqlite3"; _db(db)
    task_id=uuid.uuid4(); project_id=uuid.uuid4(); section_id=uuid.uuid4()
    manifest=tmp_path/"locations.json"
    _manifest(manifest, task_id=task_id, project_id=project_id, section_id=section_id)
    first=tmp_path/"one.ndjson"; second=tmp_path/"two.ndjson"
    assert export_legacy_source(database=db, location_manifest=manifest, output=first)==1
    assert export_legacy_source(database=db, location_manifest=manifest, output=second)==1
    assert first.read_bytes()==second.read_bytes()
    record=next(iter_source(first))
    assert record.error is None and record.spec.task_id==task_id
    assert record.spec.title=="Title" and record.spec.body=="Body"


def test_legacy_source_exports_completed_operation_and_attempt_history(tmp_path):
    db = tmp_path / "live.sqlite3"
    _db(db)
    operation_id, lease_id = uuid.uuid4(), uuid.uuid4()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (str(operation_id), "123", "planning", "completed",
         "2026-08-03T08:55:00+00:00", "2026-08-03T08:56:00+00:00",
         "terminal", "planning_handoff_confirmed"),
    )
    conn.execute(
        "INSERT INTO service_leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(lease_id), str(operation_id), "123", "owner-1", "legacy-run-1",
         "2026-08-03T08:55:00+00:00", "2026-08-03T09:05:00+00:00",
         "2026-08-03T08:56:00+00:00", "actor", 1, None),
    )
    conn.commit(); conn.close()
    manifest = tmp_path / "locations.json"
    _manifest(manifest, task_id=uuid.uuid4(), project_id=uuid.uuid4(), section_id=uuid.uuid4())
    source = tmp_path / "out.ndjson"
    export_legacy_source(database=db, location_manifest=manifest, output=source)
    parsed = next(iter_source(source))
    assert parsed.error is None and parsed.spec is not None
    history = parsed.spec.operation_history
    assert [item.operation_id for item in history.operations] == [operation_id]
    assert [(item.lease_id, item.actor_attempt_sequence) for item in history.leases] == [(lease_id, 1)]


def test_legacy_source_rejects_uncertain_operation_even_with_terminal_evidence(tmp_path):
    db = tmp_path / "live.sqlite3"
    _db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "123", "planning", "uncertain",
         "2026-08-03T08:55:00+00:00", "2026-08-03T08:56:00+00:00",
         "terminal", "planning_handoff_confirmed"),
    )
    conn.commit(); conn.close()
    manifest = tmp_path / "locations.json"
    _manifest(manifest, task_id=uuid.uuid4(), project_id=uuid.uuid4(), section_id=uuid.uuid4())
    with pytest.raises(LegacySourceError, match="terminal completed/cancelled"):
        export_legacy_source(database=db, location_manifest=manifest, output=tmp_path / "out.ndjson")


def test_legacy_source_rejects_incomplete_location_corpus(tmp_path):
    db=tmp_path/"live.sqlite3"; _db(db)
    manifest=tmp_path/"locations.json"; manifest.write_text('{"tasks":{}}')
    with pytest.raises(LegacySourceError, match="corpus mismatch"):
        export_legacy_source(database=db, location_manifest=manifest, output=tmp_path/"out")
