from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_dashboard import (
    HTML,
    JSON_PATH,
    PAGE_PATH,
    coordinator_handoff,
    dashboard_snapshot,
    handler,
)
from pr_lifecycle_projection import SCHEMA
import pr_lifecycle


def projection(now: datetime) -> dict:
    return {
        "schema": SCHEMA,
        "repository": "marcogallotta/ai-tools",
        "reconciled_at": now.isoformat(),
        "pull_requests": [{
            "number": 7, "title": "candidate", "head": "a" * 40,
            "state": "review_ready", "state_label": "REVIEW READY",
        }],
        "tasks": [],
        "queues": {"Ready": [], "In Progress": [], "Review": [7], "Integration": [], "Blocked": [], "Decision": [], "Recent": []},
        "state_drift": [],
        "controller": {"status": "running"},
        "full_regression": {"conclusion": "success", "head_sha": "b" * 40},
        "current_main_corrective_owners": [],
        "coordinator_actions": [],
    }


def test_dashboard_staleness_uses_last_successful_reconciliation_and_controller_health():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    fresh = dashboard_snapshot(projection(now - timedelta(seconds=119)), now=now)
    assert fresh["dashboard"]["stale"] is False
    old = dashboard_snapshot(projection(now - timedelta(seconds=121)), now=now)
    assert old["dashboard"]["stale"] is True
    failed = projection(now)
    failed["controller"]["status"] = "failed"
    assert dashboard_snapshot(failed, now=now)["dashboard"]["stale"] is True


def test_one_copy_ready_handoff_aggregates_actions_and_drift_with_live_warning():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    value = projection(now)
    value["coordinator_actions"] = [{"pr": 7, "action": "route Review", "head": "a" * 40}]
    value["state_drift"] = [{"pr": 8, "conflict": "head moved", "repair_owner": "Review"}]
    handoff = coordinator_handoff(value)
    assert handoff is not None
    assert "re-read live GitHub and Asana" in handoff
    assert "PR #7" in handoff and "PR #8" in handoff
    assert dashboard_snapshot(value, now=now)["dashboard"]["coordinator_handoff"] == handoff


def test_page_is_static_read_only_and_polls_same_origin_without_credentials():
    assert JSON_PATH in HTML and "setInterval(refresh,10000)" in HTML
    assert "GITHUB_TOKEN" not in HTML and "ASANA" not in HTML
    assert "navigator.clipboard.writeText" in HTML


def test_loopback_handler_serves_page_snapshot_and_rejects_mutation(tmp_path):
    now = datetime.now(timezone.utc)
    snapshot = tmp_path / "lifecycle.json"
    snapshot.write_text(json.dumps(projection(now)), encoding="utf-8")
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(snapshot))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", PAGE_PATH)
        response = connection.getresponse()
        assert response.status == 200 and b"Dish lifecycle desk" in response.read()
        connection.request("GET", JSON_PATH)
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200 and body["dashboard"]["read_only"] is True
        connection.request("POST", JSON_PATH, body=b"{}")
        response = connection.getresponse()
        assert response.status == 405 and response.read() == b"read-only\n"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_invalid_projection_fails_closed(tmp_path):
    snapshot = tmp_path / "lifecycle.json"
    snapshot.write_text('{"schema":"wrong"}', encoding="utf-8")
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(snapshot))
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", JSON_PATH)
        response = connection.getresponse()
        assert response.status == 503
        assert "projection unavailable" in json.loads(response.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_dashboard_tool_refuses_non_loopback_bind():
    from pr_lifecycle_dashboard import main
    with pytest.raises(SystemExit):
        main(["--bind", "0.0.0.0"])


def test_projection_health_reads_controller_and_latest_full_regression(monkeypatch):
    class GitHub:
        def full_regression_runs(self):
            return {"workflow_runs": [{
                "id": 42, "status": "completed", "conclusion": "success",
                "head_sha": "c" * 40, "updated_at": "2026-08-18T00:00:00Z",
            }]}

    class Engine:
        github = GitHub()

    monkeypatch.setattr(pr_lifecycle.pr_lifecycle_controller, "_paths", lambda: {"state": Path("unused")})
    monkeypatch.setattr(pr_lifecycle.pr_lifecycle_controller, "_snapshot", lambda paths: {"status": "running"})
    controller, regression = pr_lifecycle._projection_health(Engine())
    assert controller == {"status": "running"}
    assert regression["id"] == 42 and regression["conclusion"] == "success"
