"""Process-launch, credential, and bounded-termination contracts for §1."""

from __future__ import annotations

import json
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dish_pg import process_failure_rehearsal as rehearsal
from tests.support.postgresql import process_failure as process_support


def test_database_credentials_are_redacted_from_persisted_process_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = (
        "postgresql+psycopg://secret-user:secret-password@127.0.0.1:5432/dish_test"
    )
    secret_values = ("secret-user", "secret-password")

    external_log = tmp_path / "external.log"
    external_record = tmp_path / "external.json"
    external = rehearsal.run_external_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", database_url],
        cwd=tmp_path,
        env=os.environ.copy(),
        log_path=external_log,
        timeout_seconds=10.0,
        termination_grace_seconds=1.0,
        label="credential-redaction",
        record_path=external_record,
    )
    assert external["final_exit_status"] == 0
    assert external["command"][-1] == (
        "postgresql+psycopg://<redacted>@127.0.0.1:5432/dish_test"
    )

    def fake_popen(command, **kwargs):
        assert command[-1] == database_url
        kwargs["stdout"].write(f"worker echoed {database_url}\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(process_support.subprocess, "Popen", fake_popen)
    command = [sys.executable, "worker.py", "--database-url", database_url]
    child = process_support._start_child(
        command,
        tmp_path=tmp_path,
        barrier=None,
        ledger=tmp_path / "ledger.json",
        scenario="credential-redaction",
        label="projection-redaction",
    )
    assert child.command == command
    child._close_log()

    process_record = json.loads(child.manifest_path.read_text(encoding="utf-8"))
    report_path = tmp_path / "report.json"
    rehearsal.write_json_atomic(
        report_path,
        {"external_command": external, "worker_process": process_record},
    )

    persisted = "\n".join(
        [
            external_record.read_text(encoding="utf-8"),
            external_log.read_text(encoding="utf-8"),
            child.manifest_path.read_text(encoding="utf-8"),
            child.log_path.read_text(encoding="utf-8"),
            report_path.read_text(encoding="utf-8"),
        ]
    )
    for secret in secret_values:
        assert secret not in persisted
    assert persisted.count("<redacted>") >= 4


def test_command_child_configuration_keeps_database_credentials_out_of_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_start_child(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(process_support, "_start_child", fake_start_child)
    dsn = "postgresql+psycopg://secret-user:secret-password@127.0.0.1:5432/dish_test"
    process_support.start_command_process(
        dsn=dsn,
        tmp_path=tmp_path,
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        output=tmp_path / "result.json",
        now=datetime.now(timezone.utc),
        arguments={"title": "Credential-safe command"},
    )

    config_path = Path(captured["command"][-1])
    persisted = config_path.read_text(encoding="utf-8")
    assert "secret-user" not in persisted
    assert "secret-password" not in persisted
    assert captured["kwargs"]["env_overrides"]["DISH_SECTION1_COMMAND_DSN"] == dsn


def test_reconciliation_child_configuration_keeps_database_credentials_out_of_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_start_child(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(process_support, "_start_child", fake_start_child)
    dsn = "postgresql+psycopg://secret-user:secret-password@127.0.0.1:5432/dish_test"
    process_support.start_reconciliation_checkpoint_process(
        dsn=dsn,
        tmp_path=tmp_path,
        ledger=tmp_path / "ledger.json",
        generation_id=uuid.uuid4(),
        corpus_identity="credential-safe-corpus",
        output=tmp_path / "result.json",
        item_count=3,
        mode="start",
    )

    config_path = Path(captured["command"][-1])
    persisted = config_path.read_text(encoding="utf-8")
    assert "secret-user" not in persisted
    assert "secret-password" not in persisted
    assert captured["kwargs"]["env_overrides"]["DISH_SECTION1_RECONCILIATION_DSN"] == dsn


def test_external_command_timeout_terminates_process_group_and_preserves_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "timeout.log"
    record_path = tmp_path / "timeout.json"
    script = (
        "import signal, subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()']); "
        "print('parent-ready', flush=True); signal.pause()"
    )
    signals: list[tuple[int, signal.Signals]] = []
    original_killpg = rehearsal.os.killpg

    def record_killpg(process_group_id: int, sent_signal: signal.Signals) -> None:
        signals.append((process_group_id, sent_signal))
        original_killpg(process_group_id, sent_signal)

    monkeypatch.setattr(rehearsal.os, "killpg", record_killpg)

    result = rehearsal.run_external_command(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ.copy(),
        log_path=log_path,
        timeout_seconds=3.0,
        termination_grace_seconds=0.5,
        label="timeout-test",
        record_path=record_path,
    )

    assert result["timed_out"] is True
    assert result["completion_state"] == "timed_out"
    assert result["termination_state"] in {"sigterm", "sigkill"}
    assert isinstance(result["final_exit_status"], int)
    assert "finite timeout" in str(result["failure"])
    assert "parent-ready" in log_path.read_text(encoding="utf-8")
    assert json.loads(record_path.read_text(encoding="utf-8")) == result
    assert signals
    assert signals[0] == (result["process_group_id"], signal.SIGTERM)
