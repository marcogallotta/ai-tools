from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import dish_tool.database_schema as database_schema


class TrackingConnection(sqlite3.Connection):
    closed_by_owner = False

    def close(self):
        self.closed_by_owner = True
        return super().close()


def test_final_database_validation_failure_closes_created_connection(monkeypatch, tmp_path):
    real_connect = sqlite3.connect
    created = []

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    def fail_validation(_conn):
        raise RuntimeError("final validation failed")

    monkeypatch.setattr(database_schema.sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(database_schema, "_validate_current_database", fail_validation)

    with pytest.raises(RuntimeError, match="final validation failed"):
        database_schema.initialize_database(tmp_path / "invalid-after-migration.sqlite3")

    assert len(created) == 1
    assert created[0].closed_by_owner is True


def test_repeated_owned_asana_clients_exit_without_worker_pool_tail(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = """
from dish_tool.backend import AsanaBackend
for _ in range(20):
    backend = AsanaBackend()
    backend.client()
    backend.close()
print('closed')
"""
    env = dict(os.environ)
    env["ASANA_PAT"] = "test-pat-token"
    env["PYTHONPATH"] = str(root)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "closed"
