from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import ModuleType
from typing import Iterator

import pytest


def _load_router_module() -> ModuleType:
    path = Path(__file__).parents[1] / "deploy" / "caddy" / "dish-action-route"
    loader = importlib.machinery.SourceFileLoader("dish_action_route", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


router = _load_router_module()


def _route_etag(dial: str) -> str:
    body = json.dumps([{"dial": dial}]).encode("utf-8")
    return f'"{router.PROXY_PATH} {router._fnv1a_32(body):08x}"'


class _CaddyFake(BaseHTTPRequestHandler):
    dial = "127.0.0.1:8766"
    etag = '"route-v1"'
    patch_count = 0
    received_if_match: str | None = None
    send_etag = True

    def do_GET(self) -> None:
        if self.path == "/openapi/action.json":
            self._json(200, {"openapi": "3.1.0"})
            return
        if self.path == router.PROXY_PATH:
            value = [{"dial": type(self).dial}]
            type(self).etag = _route_etag(type(self).dial)
            headers = {"Etag": type(self).etag} if type(self).send_etag else None
            self._json(200, value, headers)
            return
        self._json(404, {"error": "not found"})

    def do_PATCH(self) -> None:
        if self.path != router.PROXY_PATH:
            self._json(404, {"error": "not found"})
            return
        type(self).received_if_match = self.headers.get("If-Match")
        if type(self).received_if_match != type(self).etag:
            self._json(412, {"error": "etag mismatch"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        type(self).dial = body[0]["dial"]
        type(self).patch_count += 1
        type(self).etag = '"route-v2"'
        self._json(200, None)

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _json(
        self,
        status: int,
        value: object,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = b"" if value is None else json.dumps(value).encode("utf-8")
        self.send_response(status)
        for key, header_value in (headers or {}).items():
            self.send_header(key, header_value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def caddy_fake() -> Iterator[str]:
    _CaddyFake.dial = "127.0.0.1:8766"
    _CaddyFake.etag = '"route-v1"'
    _CaddyFake.patch_count = 0
    _CaddyFake.received_if_match = None
    _CaddyFake.send_etag = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaddyFake)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_switch_preflights_uses_etag_and_confirms_readback(caddy_fake: str) -> None:
    target = router.RouteTarget("prod", caddy_fake, "127.0.0.1:8776")

    result = router.set_route(caddy_fake, target)

    assert result == {
        "environment": "prod",
        "upstream": "127.0.0.1:8776",
        "result": "switched",
    }
    assert _CaddyFake.dial == "127.0.0.1:8776"
    assert _CaddyFake.patch_count == 1
    assert _CaddyFake.received_if_match == _route_etag("127.0.0.1:8766")


def test_switch_is_idempotent_when_route_is_already_selected(caddy_fake: str) -> None:
    target = router.RouteTarget("test", caddy_fake, "127.0.0.1:8766")

    result = router.set_route(caddy_fake, target)

    assert result["result"] == "unchanged"
    assert _CaddyFake.patch_count == 0


def test_switch_reconstructs_caddy_etag_when_urllib_cannot_see_trailer(
    caddy_fake: str,
) -> None:
    _CaddyFake.send_etag = False
    target = router.RouteTarget("prod", caddy_fake, "127.0.0.1:8776")

    result = router.set_route(caddy_fake, target)

    assert result["result"] == "switched"
    assert _CaddyFake.patch_count == 1
    assert _CaddyFake.received_if_match == _route_etag("127.0.0.1:8766")


def test_production_cli_requires_both_authorizations(capsys: pytest.CaptureFixture[str]) -> None:
    assert router.main(["set", "prod"]) == 2
    assert "--authorize-route-change" in capsys.readouterr().err

    assert router.main(["set", "prod", "--authorize-route-change"]) == 2
    assert "--authorize-production-cutover" in capsys.readouterr().err
