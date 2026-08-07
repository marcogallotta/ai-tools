from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from dish_pg.importer import iter_source
from dish_pg.legacy_source import LegacySourceError, export_legacy_source


def _db(path):
    conn=sqlite3.connect(path)
    conn.execute("""CREATE TABLE task_content_state(
        task_gid TEXT PRIMARY KEY,last_confirmed_identity TEXT,last_confirmed_title TEXT,
        last_confirmed_notes TEXT,schema_version TEXT,confirmed_at TEXT)""")
    conn.execute("INSERT INTO task_content_state VALUES (?,?,?,?,?,?)",
                 ("123","id-1","Title","Body","schema-1","2026-08-03T09:00:00+00:00"))
    conn.commit(); conn.close()


def test_legacy_source_is_deterministic_and_importer_compatible(tmp_path):
    db=tmp_path/"live.sqlite3"; _db(db)
    task_id=uuid.uuid4(); project_id=uuid.uuid4(); section_id=uuid.uuid4()
    manifest=tmp_path/"locations.json"
    manifest.write_text(json.dumps({"tasks":{"123":{
        "task_id":str(task_id),"project_ids":[str(project_id)],"section_id":str(section_id),
        "section_gid":"901",
        "completed":False,"observed_at":"2026-08-03T09:01:00+00:00"}}}))
    first=tmp_path/"one.ndjson"; second=tmp_path/"two.ndjson"
    assert export_legacy_source(database=db, location_manifest=manifest, output=first)==1
    assert export_legacy_source(database=db, location_manifest=manifest, output=second)==1
    assert first.read_bytes()==second.read_bytes()
    record=next(iter_source(first))
    assert record.error is None and record.spec.task_id==task_id
    assert record.spec.title=="Title" and record.spec.body=="Body"


def test_legacy_source_rejects_incomplete_location_corpus(tmp_path):
    db=tmp_path/"live.sqlite3"; _db(db)
    manifest=tmp_path/"locations.json"; manifest.write_text('{"tasks":{}}')
    with pytest.raises(LegacySourceError, match="corpus mismatch"):
        export_legacy_source(database=db, location_manifest=manifest, output=tmp_path/"out")
