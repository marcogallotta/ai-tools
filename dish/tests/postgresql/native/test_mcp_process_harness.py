from __future__ import annotations

import signal

import pytest

from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.mcp_process import DisposableMCPProcess


@pytest.mark.native_postgresql
def test_disposable_native_mcp_process_creates_reads_and_tears_down(tmp_path) -> None:
    harness = DisposableMCPProcess(base_dsn=postgresql_dsn(), root=tmp_path)
    try:
        harness.start()
        created, readback = harness.create_readback()

        assert created["ok"] is True
        assert created["command"] == "create"
        assert readback["ok"] is True
        assert readback["command"] == "read"
        assert readback["data"]["dish_id"] == created["data"]["dish_id"]
        assert readback["data"]["title"] == "Disposable MCP process proof"
    finally:
        receipt = harness.stop() if harness.process is not None else None

    assert receipt is not None
    assert receipt.exit_code in {0, -signal.SIGTERM}
    assert receipt.forced_kill is False
    assert receipt.descendants_remaining == ()
    assert receipt.database_name.startswith("dish_mcp_")
    assert receipt.database_name.endswith("_test")
    assert receipt.database_dropped is True
