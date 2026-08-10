from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.client import HTTPConnection
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer

from .runtime import AcceptanceRuntime

ORIGIN = "https://dish.example.test"


class _Service:
    def __init__(self, root: Path) -> None:
        self.config = ServiceConfig(
            db_path=root / "legacy.sqlite3",
            honest_root=root,
            agent_token="stage7-agent-token-long-enough",
            admin_token="stage7-admin-token-long-enough",
            action_token="stage7-action-token-long-enough",
        )


@dataclass(slots=True)
class NetworkAudit:
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[str] = field(default_factory=list)
    responses: list[tuple[int, str]] = field(default_factory=list)
    redirects: list[tuple[int, str, str]] = field(default_factory=list)

    def unexpected_http_errors(self, allowed: Iterable[tuple[int, str]] = ()) -> list[tuple[int, str]]:
        accepted = list(allowed)
        return [
            item for item in self.responses
            if item[0] >= 400 and not any(item[0] == status and marker in item[1] for status, marker in accepted)
        ]


class ProductionBridge:
    def __init__(self, *, static_root: Path, scratch_root: Path) -> None:
        self.runtime = AcceptanceRuntime(static_root, origin=ORIGIN)
        self.server = DishHTTPServer(
            ("127.0.0.1", 0),
            _Service(scratch_root),
            surface_mode="private",
            frontend_runtime=self.runtime,
        )
        self.transport_failures: dict[str, int] = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="stage7-private-http")
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def install(self, context) -> None:
        context.route(f"{ORIGIN}/**", self._handle)

    def fail_transport(self, path: str, *, count: int = 1) -> None:
        self.transport_failures[path] = self.transport_failures.get(path, 0) + count

    def _handle(self, route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        remaining = self.transport_failures.get(parsed.path, 0)
        if remaining:
            if remaining == 1:
                self.transport_failures.pop(parsed.path, None)
            else:
                self.transport_failures[parsed.path] = remaining - 1
            route.abort("connectionfailed")
            return
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection = HTTPConnection(*self.server.server_address, timeout=5)
        connection.putrequest(request.method, path, skip_host=True)
        connection.putheader("Host", parsed.netloc)
        headers = request.all_headers()
        forwarded = {
            "accept", "content-type", "content-length", "cookie", "origin", "referer",
            "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "x-dish-frontend-contract", "x-dish-csrf",
        }
        for name, value in headers.items():
            if name.lower() in forwarded and name.lower() != "content-length":
                connection.putheader(name, value)
        body = request.post_data_buffer
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        payload = response.read()
        response_headers = response.getheaders()
        status = response.status
        connection.close()
        headers_out: dict[str, str] = {}
        for name, value in response_headers:
            if name.lower() in {"connection", "content-length"}:
                continue
            headers_out[name] = value
        route.fulfill(status=status, headers=headers_out, body=payload)


def attach_network_audit(page) -> NetworkAudit:
    audit = NetworkAudit()
    page.on("console", lambda msg: audit.console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda error: audit.page_errors.append(str(error)))
    page.on("requestfailed", lambda request: audit.request_failures.append(f"{request.method} {request.url}: {request.failure}"))
    page.on("response", lambda response: audit.responses.append((response.status, response.url)))
    page.on(
        "response",
        lambda response: audit.redirects.append((response.status, response.url, response.headers.get("location", "")))
        if 300 <= response.status < 400 else None,
    )
    return audit
