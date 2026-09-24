from __future__ import annotations

import json
import signal

import httpx2
import pytest

from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.mcp_public_edge import (
    GITHUB_CLIENT_SECRET,
    PUBLIC_BASE,
    PUBLIC_RESOURCE,
    DisposablePublicEdge,
)


@pytest.mark.native_postgresql
def test_public_oauth_and_caddy_journey_reaches_native_mcp_and_rejects_wrong_owner(
    tmp_path,
) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    edge = DisposablePublicEdge(base_dsn=dsn, root=tmp_path)
    try:
        edge.start()
        with httpx2.Client(trust_env=False, follow_redirects=False) as client:
            challenge = client.post(f"{edge.edge_base}/mcp", json={})
            metadata = client.get(
                f"http://127.0.0.1:{edge.caddy_port}"
                "/.well-known/oauth-protected-resource/dish/mcp"
            )
            authorization = client.get(
                f"http://127.0.0.1:{edge.caddy_port}"
                "/.well-known/oauth-authorization-server/dish"
            )
            slash = client.post(f"{edge.edge_base}/mcp/", json={})

        assert challenge.status_code == 401
        assert (
            challenge.headers["www-authenticate"]
            == 'Bearer resource_metadata="https://dish-mcp.example.test/'
            '.well-known/oauth-protected-resource/dish/mcp"'
        )
        assert metadata.json() == {
            "resource": PUBLIC_RESOURCE,
            "authorization_servers": [PUBLIC_BASE],
            "scopes_supported": [],
            "bearer_methods_supported": ["header"],
        }
        assert authorization.json()["issuer"] == PUBLIC_BASE
        assert authorization.json()["authorization_endpoint"] == f"{PUBLIC_BASE}/authorize"
        assert authorization.json()["token_endpoint"] == f"{PUBLIC_BASE}/token"
        assert authorization.json()["registration_endpoint"] == f"{PUBLIC_BASE}/register"
        assert slash.status_code == 307
        assert slash.headers["location"] == PUBLIC_RESOURCE

        accepted_token, accepted = edge.oauth_token()
        sections = edge.sections(accepted_token)
        assert sections["ok"] is True
        assert sections["command"] == "sections"
        assert accepted["callback"].startswith("http://127.0.0.1:")
        assert accepted["callback"].endswith("/callback")
        assert accepted["state"] == "client-state"
        assert accepted["issuer"] == PUBLIC_BASE
        assert accepted["upstream_redirect"] == f"{PUBLIC_BASE}/auth/callback"

        rejected_token, _ = edge.oauth_token()
        with httpx2.Client(trust_env=False) as client:
            rejected = client.post(
                f"{edge.edge_base}/mcp",
                headers={
                    "Authorization": f"Bearer {rejected_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "rejected-owner", "version": "1"},
                    },
                },
            )
        assert rejected.status_code == 401

        observations = edge.observations()
        assert [(row["method"], row["path"]) for row in observations] == [
            ("GET", "/authorize"),
            ("POST", "/token"),
            ("GET", "/authorize"),
            ("POST", "/token"),
        ]
        assert all(
            row["redirect_uri"] == f"{PUBLIC_BASE}/auth/callback"
            for row in observations
        )
        assert [
            row["resource"] for row in observations if row["path"] == "/authorize"
        ] == [PUBLIC_RESOURCE, PUBLIC_RESOURCE]
    finally:
        receipt = edge.stop() if edge.process is not None else None

    assert receipt is not None
    assert receipt.exit_code in {0, -signal.SIGTERM}
    assert receipt.forced_kill is False
    assert receipt.descendants_remaining == ()
    assert receipt.database_dropped is True
    diagnostics = edge.log_path.read_text(encoding="utf-8")
    assert GITHUB_CLIENT_SECRET not in diagnostics
    assert "github-accepted" not in diagnostics
    assert "github-rejected" not in diagnostics
    assert accepted_token not in diagnostics
    assert rejected_token not in diagnostics
    json.loads(edge.caddy_config_path.read_text(encoding="utf-8"))
