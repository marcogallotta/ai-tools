"""Real isolated rehearsal listeners using the production HTTP/MCP applications.

The TEST trust domain uses its own cryptographically verified JWTs. It never
contacts GitHub or reads production OAuth credentials. This does not commission
production GitHub OAuth or public routing.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("backend", "mcp"))
    args = parser.parse_args()
    if os.environ.get("DISH_PROFILE") != "test":
        raise RuntimeError("rehearsal runtime requires isolated TEST profile")
    root = Path(os.environ["DISH_RELEASE_TEST_ROOT"]).resolve()
    for name in (
        "DISH_PG_AUTHORITY_STATE_DIR",
        "DISH_MCP_HONEST_STORE",
        "DISH_MCP_PLANNING_CONTEXT_DB",
        "FASTMCP_HOME",
    ):
        if not Path(os.environ[name]).resolve().is_relative_to(root):
            raise RuntimeError("rehearsal store escapes owned isolation root")
    from dish_service.native_mcp_server import (
        native_adapter_from_environment,
        runtime_service_from_environment,
    )

    if args.component == "backend":
        from dish_service.config import ServiceConfig
        from dish_service.http import DishHTTPServer

        service = runtime_service_from_environment(
            authenticated_owner_id="release-test"
        )
        service.config = replace(
            ServiceConfig.from_environment(os.environ), legacy_writer_fence_path=None
        )
        server = DishHTTPServer(
            ("127.0.0.1", int(os.environ["DISH_SERVICE_PORT"])),
            service,
            surface_mode="private",
        )
        try:
            server.serve_forever()
        finally:
            server.server_close()
            service.close()
    else:
        import uvicorn
        from dish_service.mcp_server import MCPAuthConfig, create_app
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        resource = os.environ["DISH_MCP_RESOURCE_URL"]
        key_file = root / "jwt-key"
        key = key_file.read_text()
        auth = JWTVerifier(
            public_key=key,
            algorithm="HS256",
            issuer="dish-release-rehearsal",
            audience=resource,
            base_url=resource.removesuffix("/mcp"),
        )
        config = MCPAuthConfig(
            "isolated",
            "isolated-unused",
            "1",
            resource,
            bind_port=int(os.environ["DISH_MCP_BIND_PORT"]),
        )
        adapter = native_adapter_from_environment(authenticated_owner_id="release-test")
        try:
            app = create_app(adapter, config, test_auth=auth)
            uvicorn.run(app, host="127.0.0.1", port=config.bind_port, access_log=False)
        finally:
            adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
