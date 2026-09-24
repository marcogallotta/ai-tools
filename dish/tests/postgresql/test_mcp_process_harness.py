from __future__ import annotations

import subprocess
import sys

import pytest

from tests.support.postgresql import mcp_process
from tests.support.postgresql.mcp_process import (
    ROOT,
    DisposableMCPError,
    DisposableMCPProcess,
)


@pytest.mark.parametrize(
    "server_args",
    (("--profile", "prod"), ("--database-name=dish_live_prod",), ("--p", "1")),
)
def test_disposable_process_rejects_protected_server_argument_overrides(
    tmp_path, server_args
) -> None:
    with pytest.raises(DisposableMCPError, match="protected option"):
        DisposableMCPProcess(
            base_dsn="postgresql+psycopg://dish_test@localhost/postgres",
            root=tmp_path,
            server_args=server_args,
        )


def test_option_like_generated_token_is_unambiguous_to_server_parser(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(mcp_process.secrets, "token_urlsafe", lambda _size: "-token")
    harness = DisposableMCPProcess(
        base_dsn="postgresql+psycopg://dish_test@localhost/postgres",
        root=tmp_path,
    )

    command_line = harness._server_command_line()
    parsed = mcp_process._parser().parse_args(command_line[3:])

    assert "--token=-token" in command_line
    assert parsed.token == "-token"


def test_start_drops_created_database_when_migration_fails(
    tmp_path, monkeypatch
) -> None:
    dropped: list[tuple[str, str]] = []
    harness = DisposableMCPProcess(
        base_dsn="postgresql+psycopg://dish_test@localhost/postgres",
        root=tmp_path,
    )
    expected_dsn = (
        "postgresql+psycopg://dish_test@localhost/" + harness.database_name
    )

    monkeypatch.setattr(mcp_process, "_create_database", lambda *_args: expected_dsn)

    def fail_migration(_dsn: str) -> None:
        raise RuntimeError("injected Alembic failure")

    monkeypatch.setattr(mcp_process, "_migrate_database", fail_migration)
    monkeypatch.setattr(
        mcp_process,
        "_drop_database",
        lambda base_dsn, database_name: dropped.append((base_dsn, database_name)),
    )

    with pytest.raises(RuntimeError, match="injected Alembic failure"):
        harness.start()

    assert harness.dsn == expected_dsn
    assert dropped == [(harness.base_dsn, harness.database_name)]


def test_stop_drops_database_and_records_receipt_after_child_already_exited(
    tmp_path, monkeypatch
) -> None:
    dropped: list[tuple[str, str]] = []
    harness = DisposableMCPProcess(
        base_dsn="postgresql+psycopg://dish_test@localhost/postgres",
        root=tmp_path,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(7)"],
        start_new_session=True,
        text=True,
    )
    harness.process = process
    harness.process_group_id = process.pid
    harness.dsn = "created"
    process.wait(timeout=5)
    monkeypatch.setattr(
        mcp_process,
        "_drop_database",
        lambda base_dsn, database_name: dropped.append((base_dsn, database_name)),
    )

    receipt = harness.stop()

    assert receipt.exit_code == 7
    assert receipt.database_dropped is True
    assert receipt.descendants_remaining == ()
    assert dropped == [(harness.base_dsn, harness.database_name)]


def test_disposable_server_process_refuses_production_identity(tmp_path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.support.postgresql.mcp_process",
            "serve",
            "--profile",
            "prod",
            "--database-name",
            "dish_live_prod",
            "--port",
            "1",
            "--token",
            "unused",
            "--owner-id",
            "192548",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 2
    assert "refuses non-TEST profile" in completed.stderr
