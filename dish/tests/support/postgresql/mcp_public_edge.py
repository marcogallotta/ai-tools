"""Hermetic public OAuth/Caddy boundary around the disposable MCP harness."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2
import uvicorn
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dish_service import mcp_server
from dish_service.native_mcp_server import native_adapter_from_environment
from tests.support.postgresql.mcp_process import (
    ROOT,
    DisposableMCPError,
    DisposableMCPProcess,
    _free_loopback_port,
    _require_test_identity,
)

PUBLIC_ORIGIN = "https://dish-mcp.example.test"
PUBLIC_BASE = f"{PUBLIC_ORIGIN}/dish"
PUBLIC_RESOURCE = f"{PUBLIC_BASE}/mcp"
CADDY_UNAVAILABLE = "public-edge conformance UNAVAILABLE: caddy executable missing"
GITHUB_CLIENT_SECRET = "stub-github-client-secret"


class PublicEdgeUnavailable(DisposableMCPError):
    """A required executable boundary is unavailable."""


class _StubGitHubVerifier(TokenVerifier):
    def __init__(self, allowed_user_id: str) -> None:
        super().__init__(required_scopes=[])
        self.allowed_user_id = allowed_user_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if token not in {"github-accepted", "github-rejected"}:
            return None
        subject = self.allowed_user_id if token.endswith("accepted") else "999999"
        return AccessToken(
            token=token,
            client_id="stub-github-client",
            scopes=[],
            subject=subject,
            claims={"iss": "stub-github"},
        )


class _StubDishGitHubProvider(mcp_server.DishGitHubProvider):
    upstream_origin = ""

    def __init__(self, *, allowed_user_id: str, **kwargs: Any) -> None:
        self.allowed_user_id = allowed_user_id
        OAuthProxy.__init__(
            self,
            upstream_authorization_endpoint=f"{self.upstream_origin}/authorize",
            upstream_token_endpoint=f"{self.upstream_origin}/token",
            upstream_client_id=str(kwargs["client_id"]),
            upstream_client_secret=str(kwargs["client_secret"]),
            token_verifier=_StubGitHubVerifier(allowed_user_id),
            base_url=kwargs["base_url"],
            issuer_url=kwargs["issuer_url"],
            jwt_signing_key=str(kwargs["client_secret"]),
            require_authorization_consent="external",
            enable_cimd=False,
        )


class _GitHubStubHandler(BaseHTTPRequestHandler):
    authorization_count = 0
    observations: Path

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _observe(self, event: dict[str, object]) -> None:
        with self.observations.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/authorize":
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        type(self).authorization_count += 1
        identity = "accepted" if self.authorization_count == 1 else "rejected"
        callback = query["redirect_uri"][0]
        location = f"{callback}?{urlencode({'code': identity, 'state': query['state'][0]})}"
        self._observe(
            {
                "method": "GET",
                "path": parsed.path,
                "redirect_uri": callback,
                "resource": query.get("resource", [None])[0],
            }
        )
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/token":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        identity = form["code"][0]
        self._observe(
            {
                "method": "POST",
                "path": parsed.path,
                "redirect_uri": form["redirect_uri"][0],
                "fields": sorted(form),
            }
        )
        payload = json.dumps(
            {
                "access_token": f"github-{identity}",
                "token_type": "bearer",
                "scope": "",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _caddy_config(*, mcp_port: int, caddy_port: int) -> dict[str, Any]:
    source = ROOT / "deploy/caddy/dish-action-router.json"
    config = json.loads(source.read_text(encoding="utf-8"))
    config["admin"] = {"disabled": True}
    server = config["apps"]["http"]["servers"]["dish_action_router"]
    server["listen"] = [f"127.0.0.1:{caddy_port}"]
    for route in server["routes"]:
        encoded = json.dumps(route)
        if '"dish_mcp_' in encoded:
            for handler in route["handle"]:
                for upstream in handler.get("upstreams", []):
                    upstream["dial"] = f"127.0.0.1:{mcp_port}"
    return config


def _serve(args: argparse.Namespace) -> int:
    _require_test_identity(profile=args.profile, database_name=args.database_name)
    observations = Path(args.observations)
    _GitHubStubHandler.observations = observations
    provider = ThreadingHTTPServer(("127.0.0.1", args.oauth_port), _GitHubStubHandler)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()

    caddy_config = Path(args.caddy_config)
    caddy_config.write_text(
        json.dumps(_caddy_config(mcp_port=args.port, caddy_port=args.caddy_port)),
        encoding="utf-8",
    )
    caddy = subprocess.Popen(
        [args.caddy, "run", "--config", str(caddy_config)],
        stdout=None,
        stderr=None,
        text=True,
    )
    adapter = native_adapter_from_environment(authenticated_owner_id=args.owner_id)
    original_provider = mcp_server.DishGitHubProvider
    _StubDishGitHubProvider.upstream_origin = f"http://127.0.0.1:{args.oauth_port}"
    mcp_server.DishGitHubProvider = _StubDishGitHubProvider
    config = mcp_server.MCPAuthConfig(
        github_client_id="stub-github-client",
        github_client_secret=GITHUB_CLIENT_SECRET,
        github_user_id=args.owner_id,
        resource_url=PUBLIC_RESOURCE,
    )
    try:
        uvicorn.run(
            mcp_server.create_app(adapter, config),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
        )
    finally:
        mcp_server.DishGitHubProvider = original_provider
        adapter.close()
        provider.shutdown()
        provider.server_close()
        if caddy.poll() is None:
            caddy.terminate()
        caddy.wait(timeout=5)
    return 0


class DisposablePublicEdge(DisposableMCPProcess):
    """Disposable database, OAuth MCP server, stub GitHub, and real Caddy."""

    def __init__(self, *, base_dsn: str, root: Path) -> None:
        caddy = shutil.which("caddy")
        if caddy is None:
            raise PublicEdgeUnavailable(CADDY_UNAVAILABLE)
        self.caddy_port = _free_loopback_port()
        self.oauth_port = _free_loopback_port()
        self.observations_path = root / "github-observations.jsonl"
        self.caddy_config_path = root / "caddy.json"
        super().__init__(
            base_dsn=base_dsn,
            root=root,
            server_module="tests.support.postgresql.mcp_public_edge",
            fastmcp_home=root / "fastmcp",
            server_args=(
                "--caddy-port",
                str(self.caddy_port),
                "--oauth-port",
                str(self.oauth_port),
                "--caddy",
                caddy,
                "--caddy-config",
                str(self.caddy_config_path),
                "--observations",
                str(self.observations_path),
            ),
        )
        self.readiness_port = self.caddy_port

    @property
    def edge_base(self) -> str:
        return f"http://127.0.0.1:{self.caddy_port}/dish"

    def start(self) -> Self:
        super().start()
        deadline = time.monotonic() + 10
        metadata = f"http://127.0.0.1:{self.caddy_port}/.well-known/oauth-authorization-server/dish"
        with httpx2.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(metadata).status_code == 200:
                        return self
                except httpx2.TransportError:
                    pass
                time.sleep(0.05)
        raise DisposableMCPError("public edge did not become ready before timeout")

    def _edge_url(self, public_url: str) -> str:
        parsed = urlparse(public_url)
        if f"{parsed.scheme}://{parsed.netloc}" != PUBLIC_ORIGIN:
            return public_url
        return f"http://127.0.0.1:{self.caddy_port}{parsed.path}" + (
            f"?{parsed.query}" if parsed.query else ""
        )

    def oauth_token(self) -> tuple[str, dict[str, str]]:
        callback = f"http://127.0.0.1:{_free_loopback_port()}/callback"
        verifier = "A" * 64
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        with httpx2.Client(trust_env=False, follow_redirects=False) as client:
            registration = client.post(
                f"{self.edge_base}/register",
                json={
                    "redirect_uris": [callback],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "client_name": "Dish public-edge conformance",
                },
            )
            registration.raise_for_status()
            client_id = registration.json()["client_id"]
            authorization = client.get(
                f"{self.edge_base}/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": callback,
                    "state": "client-state",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "resource": PUBLIC_RESOURCE,
                },
            )
            upstream = client.get(authorization.headers["location"])
            callback_response = client.get(self._edge_url(upstream.headers["location"]))
            client_callback = urlparse(callback_response.headers["location"])
            callback_query = parse_qs(client_callback.query)
            token = client.post(
                f"{self.edge_base}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": callback_query["code"][0],
                    "redirect_uri": callback,
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": PUBLIC_RESOURCE,
                },
            )
            token.raise_for_status()
            return token.json()["access_token"], {
                "callback": callback,
                "state": callback_query["state"][0],
                "issuer": callback_query["iss"][0],
                "upstream_redirect": parse_qs(urlparse(authorization.headers["location"]).query)[
                    "redirect_uri"
                ][0],
            }

    async def _sections(self, token: str) -> dict[str, Any]:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, trust_env=False
            ) as client,
            streamable_http_client(
                f"{self.edge_base}/mcp", http_client=client
            ) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "dish_sections",
                {
                    "client": {"run_id": str(uuid.uuid4())},
                    "arguments": {"agent": "codex"},
                },
            )
            if result.is_error or not isinstance(result.structured_content, dict):
                raise DisposableMCPError(f"dish_sections failed: {result}")
            return result.structured_content

    def sections(self, token: str) -> dict[str, Any]:
        return asyncio.run(self._sections(token))

    def observations(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.observations_path.read_text(encoding="utf-8").splitlines()
        ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--database-name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--caddy-port", type=int, required=True)
    parser.add_argument("--oauth-port", type=int, required=True)
    parser.add_argument("--caddy", required=True)
    parser.add_argument("--caddy-config", required=True)
    parser.add_argument("--observations", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return _serve(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
