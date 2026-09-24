from __future__ import annotations

import pytest

from tests.conftest import _native_postgresql_summary
from tests.support.postgresql import mcp_public_edge


def test_public_edge_reports_missing_caddy_as_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_public_edge.shutil, "which", lambda _name: None)

    with pytest.raises(
        mcp_public_edge.PublicEdgeUnavailable,
        match="public-edge conformance UNAVAILABLE: caddy executable missing",
    ):
        mcp_public_edge.DisposablePublicEdge(
            base_dsn="postgresql+psycopg://dish_test@localhost/postgres",
            root=tmp_path,
        )


def test_missing_caddy_reason_is_native_unavailable_not_failure() -> None:
    nodeid = (
        "tests/postgresql/native/test_mcp_public_edge.py::"
        "test_public_oauth_and_caddy_journey_reaches_native_mcp_and_rejects_wrong_owner"
    )

    class Config:
        def __init__(self) -> None:
            self._native_postgresql_state = {
                "selected_nodeids": [nodeid],
                "outcomes": {nodeid: "skipped"},
                "skip_reasons": {
                    nodeid: f"Skipped: {mcp_public_edge.CADDY_NATIVE_UNAVAILABLE}"
                },
            }

    summary = _native_postgresql_summary(Config())

    assert summary["failed"] == 0
    assert summary["skipped"] == 1
    assert summary["unavailable"] == 1
    assert summary["unavailable_nodeids"] == [nodeid]
