from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from dish_pg.postgres_service import _section4_control_point
from dish_pg.production_shaped_rehearsal import (
    PhaseRecorder,
    ProductionShapedError,
    _cleanup_owned_resources,
    _safe_child_env,
    main,
)
from dish_pg.production_shaped_runtime import BarrierServer, ServiceRuntimeClient, reach_barrier


def _record() -> dict[str, object]:
    return {
        "task_id": "77777777-7777-4777-8777-777777777777",
        "asana_task_gid": "9000000000000001",
        "title": "Sanitized production-shaped task",
        "body": "Synthetic text with realistic length and no production locator.",
        "identity_scheme": "sanitized-v1",
        "content_identity": "sanitized-content-1",
        "project_ids": ["1ae6e7ba-31e3-5dc5-9565-4ea37b49ac97"],
        "section_id": "8b5bfb31-b986-5116-a207-569a5ba95907",
        "completed": False,
        "observed_at": "2026-08-06T12:00:00Z",
    }


def _corpus_and_manifest(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    corpus = tmp_path / "sanitized.ndjson"
    corpus.write_text(json.dumps(_record(), sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(corpus.read_bytes()).hexdigest()
    value: dict[str, object] = {
        "schema": "dish-production-shaped-corpus-manifest-v1",
        "sanitized": True,
        "resource_scope": "local_or_test_only",
        "production_contact_prohibited": True,
        "contains_production_credentials": False,
        "corpus_sha256": digest,
        "record_count": 1,
        "deployment_identity": {"identity": "deployment-test"},
        "source_manifest": {"identity": "source-test"},
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return corpus, manifest, value


def test_section4_runtime_and_fault_phases_are_implemented_without_sleeps():
    rehearsal = (
        Path(__file__).resolve().parents[2]
        / "dish_pg"
        / "production_shaped_rehearsal.py"
    ).read_text(encoding="utf-8")
    runtime = (
        Path(__file__).resolve().parents[2]
        / "dish_pg"
        / "production_shaped_runtime.py"
    ).read_text(encoding="utf-8")
    assert "--service-entry-point" in rehearsal
    assert "_service_process_loss_scenario" in rehearsal
    assert "_service_database_disconnect_scenario" in rehearsal
    assert "_projection_process_loss_scenario" in rehearsal
    assert "_projection_database_disconnect_scenario" in rehearsal
    assert "_reconciliation_process_loss_scenario" in rehearsal
    assert "_reconciliation_database_disconnect_scenario" in rehearsal
    assert "BarrierServer" in rehearsal
    assert "time.sleep(" not in rehearsal
    assert "dish-section4-runtime-v1" not in runtime
    assert "--postgresql-test-runtime" in runtime
    assert "not_implemented" not in {item.status for item in PhaseRecorder().items}


def test_postgresql_test_service_control_point_uses_section4_barrier(
    tmp_path, monkeypatch
):
    import threading

    request_id = uuid.uuid4()
    path = tmp_path / "service-barrier.sock"
    monkeypatch.setenv(
        "DISH_SECTION4_SERVICE_CONTROL_POINT", "after_execute_before_commit"
    )
    monkeypatch.setenv("DISH_SECTION4_SERVICE_REQUEST_ID", str(request_id))
    monkeypatch.setenv("DISH_SECTION4_SERVICE_BARRIER_SOCKET", str(path))
    completed: list[str] = []
    with BarrierServer(path) as server:
        thread = threading.Thread(
            target=lambda: (
                _section4_control_point(
                    point="after_execute_before_commit",
                    request_id=request_id,
                    command="create",
                ),
                completed.append("done"),
            ),
            daemon=True,
        )
        thread.start()
        event = server.wait("service_after_execute_before_commit", timeout_seconds=2.0)
        assert event.payload == {
            "command": "create",
            "command_request_id": str(request_id),
        }
        assert completed == []
        event.release()
        thread.join(2.0)
    assert completed == ["done"]


def test_explicit_barrier_round_trip_uses_control_message(tmp_path):
    import threading

    path = tmp_path / "barrier.sock"
    completed: list[str] = []
    with BarrierServer(path) as server:
        thread = threading.Thread(
            target=lambda: (reach_barrier(path, "checkpoint", {"value": 7}), completed.append("done")),
            daemon=True,
        )
        thread.start()
        event = server.wait("checkpoint", timeout_seconds=2.0)
        assert event.payload == {"value": 7}
        assert event.pid > 0
        assert completed == []
        event.release()
        thread.join(2.0)
    assert completed == ["done"]


def test_service_runtime_client_uses_existing_dish_service_http_path(tmp_path, monkeypatch):
    spawned = []

    class FakeProcess:
        def poll(self):
            return None

    class FakeChild:
        running = True
        process = FakeProcess()
        log_path = tmp_path / "service.log"

        def evidence(self):
            return {"pid": 123, "command": ["dish-service", "--postgresql-test-runtime"]}

        def terminate(self, *, grace_seconds):
            self.running = False
            return {"stopped": True, "grace_seconds": grace_seconds}

        def kill_for_fault(self):
            self.running = False
            return {"stopped": True, "signal": "SIGKILL"}

    def fake_spawn(**kwargs):
        spawned.append(kwargs)
        return FakeChild()

    monkeypatch.setattr(
        "dish_pg.production_shaped_runtime.ManagedChild.spawn", fake_spawn
    )
    client = ServiceRuntimeClient(
        entry_point=Path(__file__).resolve().parents[2] / "dish-service",
        database_url="postgresql+psycopg://dish@127.0.0.1:56640/dish_section4_test",
        expected_database="dish_section4_test",
        expected_schema_head="0002_core_authority_model",
        expected_release="dish@test",
        generation_id=str(uuid.UUID(int=1)),
        owner_id="section4-test-owner",
        run_id=str(uuid.UUID(int=2)),
        evidence_dir=tmp_path,
        cwd=Path(__file__).resolve().parents[2],
        env=_safe_child_env(),
        log_path=tmp_path / "runtime.log",
        python_executable=sys.executable,
    )
    monkeypatch.setattr(
        client,
        "_wait_health",
        lambda **_kwargs: {"ok": True, "identity": {"database": "dish_section4_test"}},
    )
    responses = iter(
        [
            (200, {"ok": True, "data": {"ok": True, "command": "sections"}}),
        ]
    )
    monkeypatch.setattr(client, "_http_json", lambda *_args, **_kwargs: next(responses))
    first = client.start()
    assert first["transport"] == "dish-service-http"
    command = spawned[0]["argv"]
    assert "--postgresql-test-runtime" in command
    assert "--expected-database" in command
    result = client.command(command="sections", arguments={}, request_id=None)
    assert result["ok"] is True
    assert client.kill_for_fault()["stopped"] is True

def test_cleanup_retains_work_root_when_child_is_not_confirmed_stopped(tmp_path):
    work_root = tmp_path / "work"
    work_root.mkdir()
    (work_root / "owned-data").write_text("retain", encoding="utf-8")
    log = work_root / "child.log"
    log.write_text("still running", encoding="utf-8")

    class Child:
        label = "stubborn-child"
        pid = 4242
        process_group_id = 4242
        log_path = log

        def terminate(self, *, grace_seconds):
            assert grace_seconds == 5.0
            return {
                "stopped": False,
                "pid": self.pid,
                "process_group_id": self.process_group_id,
                "log_path": str(self.log_path),
                "cleanup_commands": ["kill -TERM -- -4242", "kill -KILL -- -4242"],
            }

        def evidence(self):
            return {
                "label": self.label,
                "pid": self.pid,
                "process_group_id": self.process_group_id,
                "log_path": str(self.log_path),
            }

    outcome = _cleanup_owned_resources(
        children=[Child()],
        clusters=[],
        engine=None,
        work_root=work_root,
        keep_resources=False,
    )
    assert outcome.failed is True
    assert outcome.work_root_removed is False
    assert work_root.is_dir()
    assert outcome.evidence[0]["cleanup"]["stopped"] is False
    assert outcome.evidence[0]["pid"] == 4242
    assert outcome.evidence[0]["log_path"] == str(log)
    assert any("work root retained" in item for item in outcome.requirements)
    assert any("kill -KILL -- -4242" in item for item in outcome.requirements)


def test_cleanup_retains_cluster_identity_when_stop_cannot_be_confirmed(tmp_path):
    work_root = tmp_path / "work"
    data = work_root / "primary-data"
    logs = data / "log"
    logs.mkdir(parents=True)
    (data / "postmaster.pid").write_text("5151\n", encoding="utf-8")
    log = logs / "postgresql.log"
    log.write_text("server remains active", encoding="utf-8")

    class RunnerStub:
        def run(self, argv, *, timeout_seconds, check):
            assert argv[-1] == "status"
            assert timeout_seconds == 10.0
            assert check is False
            return subprocess.CompletedProcess(argv, 0, "server is running", "")

    class ClusterStub:
        name = "section4-primary"
        data_dir = data
        socket_dir = work_root / "primary-socket"
        port = 56640
        binaries = {"pg_ctl": Path("/opt/postgresql/bin/pg_ctl")}
        runner = RunnerStub()

        def stop(self):
            raise RuntimeError("pg_ctl fast stop failed")

    outcome = _cleanup_owned_resources(
        children=[],
        clusters=[ClusterStub()],
        engine=None,
        work_root=work_root,
        keep_resources=False,
    )
    record = outcome.evidence[0]
    assert outcome.failed is True
    assert work_root.is_dir()
    assert record["stopped"] is False
    assert record["pid"] == 5151
    assert record["port"] == 56640
    assert record["data_path"] == str(data)
    assert record["logs"] == [str(log)]
    assert record["pg_ctl_status_returncode"] == 0
    assert record["cleanup_commands"][-1].endswith("status")
    assert any("manual PostgreSQL cleanup" in item for item in outcome.requirements)


def test_cleanup_removes_work_root_only_after_confirmed_stop(tmp_path):
    work_root = tmp_path / "work"
    work_root.mkdir()
    outcome = _cleanup_owned_resources(
        children=[],
        clusters=[],
        engine=None,
        work_root=work_root,
        keep_resources=False,
    )
    assert outcome.failed is False
    assert outcome.work_root_removed is True
    assert not work_root.exists()

def test_main_never_overwrites_existing_report(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text("sentinel\n", encoding="utf-8")
    rc = main(["--report", str(report)])
    assert rc == 2
    assert report.read_text(encoding="utf-8") == "sentinel\n"
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "failed"
    assert any("refusing overwrite" in item for item in emitted["blocked_scenarios"])


def test_repository_input_identity_is_canonical(tmp_path):
    corpus, manifest, _ = _corpus_and_manifest(tmp_path)
    honest = tmp_path / "honest"
    honest.mkdir()
    args = argparse.Namespace(
        report=tmp_path / "report.json",
        evidence_dir=tmp_path / "evidence",
        work_root=tmp_path / "work",
        corpus=corpus,
        corpus_manifest=manifest,
        honest_repo=honest,
        honest_commit="a" * 40,
        repository_input_identity="free-form-label",
        port_base=56640,
        pg_bin=None,
    )
    from dish_pg.production_shaped_rehearsal import _required_args

    with pytest.raises(ProductionShapedError, match="repository-input-identity"):
        _required_args(args)

def test_entrypoint_uses_production_shaped_main():
    script = Path(__file__).resolve().parents[2] / "scripts" / "dish-pg-production-shaped-rehearsal"
    text = script.read_text(encoding="utf-8")
    assert "dish_pg.production_shaped_rehearsal import main" in text
    assert script.stat().st_mode & 0o111
