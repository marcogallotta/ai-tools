"""Real isolated rehearsal listeners using the production HTTP/MCP applications.

The TEST trust domain uses its own cryptographically verified JWTs. It never
contacts GitHub or reads production OAuth credentials. This does not commission
production GitHub OAuth or public routing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

# The fixture launcher is separate from immutable artifacts. All application
# modules and dependencies come from the selected release working directory.
sys.path.insert(0, str(Path.cwd().resolve()))


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
        runtime_service_from_environment,
    )
    from dish_service import honest_store

    honest_store.REMOTE_URL = str(root / "honest/upstream.git")

    if args.component == "backend":
        from dish_service.config import ServiceConfig
        from dish_service.http import DishHTTPServer, DishRequestHandler

        service = runtime_service_from_environment(
            authenticated_owner_id="release-test"
        )
        service.config = replace(
            ServiceConfig.from_env(), legacy_writer_fence_path=None
        )
        server = DishHTTPServer(
            ("127.0.0.1", int(os.environ["DISH_SERVICE_PORT"])),
            service,
            surface_mode="private",
        )
        action = DishHTTPServer(
            ("127.0.0.1", int(os.environ["DISH_ACTION_PORT"])),
            service,
            surface_mode="private"
            if (root / "broken-action-interface").exists()
            else "action",
        )

        class ObservedActionHandler(DishRequestHandler):
            def _write_json(self, status, payload):
                super()._write_json(status, payload)
                event = {
                    "at": time.time(),
                    "path": self.path,
                    "status": int(status),
                    "ok": payload.get("ok") if isinstance(payload, dict) else None,
                    **self.server.runtime_identity,
                }
                fd = os.open(
                    root / "action-reads.jsonl",
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                with os.fdopen(fd, "w") as stream:
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())

        action.RequestHandlerClass = ObservedActionHandler
        thread = threading.Thread(target=action.serve_forever, daemon=True)
        thread.start()
        try:
            server.serve_forever()
        finally:
            action.shutdown()
            action.server_close()
            server.server_close()
            service.close()
    else:
        import uvicorn
        from dish_service.mcp_server import (
            MCPAuthConfig,
            create_app,
            backend_from_environment,
        )
        from fastmcp.server.auth.providers.jwt import JWTVerifier
        from fastmcp.server.auth import RemoteAuthProvider

        resource = os.environ["DISH_MCP_RESOURCE_URL"]
        key_file = root / "jwt-key"
        key = key_file.read_text()
        verifier = JWTVerifier(
            public_key=key,
            algorithm="HS256",
            issuer="dish-release-rehearsal",
            audience=resource,
            base_url=resource.removesuffix("/mcp"),
        )
        auth = RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[resource.removesuffix("/mcp")],
            base_url=resource.removesuffix("/mcp"),
        )
        config = MCPAuthConfig(
            "isolated",
            "isolated-unused",
            "1",
            resource,
            bind_port=int(os.environ["DISH_MCP_BIND_PORT"]),
        )
        adapter = backend_from_environment(config)
        try:
            app = create_app(adapter, config, test_auth=auth)
            uvicorn.run(app, host="127.0.0.1", port=config.bind_port, access_log=False)
        finally:
            close = getattr(adapter, "close", None)
            if close:
                close()


if __name__ == "__main__":
    raise SystemExit(main())
