from __future__ import annotations

import pytest

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
