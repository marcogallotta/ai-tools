from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest

from tests.support.thread_teardown import start_server_thread, stop_server


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
ROOT = Path(__file__).parents[1]
ROUTER_CONFIG = ROOT / "deploy/caddy/dish-action-router.json"


class _CaddyFake(BaseHTTPRequestHandler):
    dials = dict(router.EXPECTED_DIALS)
    effective_config = json.loads(ROUTER_CONFIG.read_text())

    def do_GET(self) -> None:
        if self.path == "/config/":
            self._json(200, type(self).effective_config)
            return
        environment = next(
            (name for name, path in router.PROXY_PATHS.items() if path == self.path),
            None,
        )
        if environment is None:
            self._json(404, {"error": "not found"})
            return
        self._json(200, [{"dial": type(self).dials[environment]}])

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def caddy_fake() -> Iterator[str]:
    _CaddyFake.dials = dict(router.EXPECTED_DIALS)
    _CaddyFake.effective_config = json.loads(ROUTER_CONFIG.read_text())
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaddyFake)
    thread = start_server_thread(server, poll_interval=0.005)
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        stop_server(server, thread)


def test_status_proves_source_config_is_effective_and_routes_are_fixed(caddy_fake: str) -> None:
    result = router.status(caddy_fake, source_config=ROUTER_CONFIG)

    assert result["mode"] == "fixed-path-split-with-comparator"
    assert result["activation_source"] == f"file:{ROUTER_CONFIG}"
    assert result["source_config_sha256"] == result["effective_config_sha256"]
    assert result["source_is_effective"] is True
    assert result["routes"] == {
        "prod": "127.0.0.1:8776",
        "test": "127.0.0.1:8766",
        "test-legacy": "127.0.0.1:8796",
    }
    assert result["status"] == "ready"


def test_status_fails_closed_on_unexpected_route(caddy_fake: str) -> None:
    _CaddyFake.dials["test"] = "127.0.0.1:9999"

    result = router.status(caddy_fake, source_config=ROUTER_CONFIG)

    assert result["status"] == "unexpected"


def test_status_fails_closed_when_stale_effective_config_differs_from_source(caddy_fake: str) -> None:
    stale = copy.deepcopy(_CaddyFake.effective_config)
    stale["apps"]["http"]["servers"]["dish_action_router"]["listen"] = [":9999"]
    _CaddyFake.effective_config = stale

    result = router.status(caddy_fake, source_config=ROUTER_CONFIG)

    assert result["routes"] == router.EXPECTED_DIALS
    assert result["source_is_effective"] is False
    assert result["source_config_sha256"] != result["effective_config_sha256"]
    assert result["status"] == "unexpected"


def test_router_service_uses_committed_file_as_only_startup_activation_source() -> None:
    unit = (ROOT / "deploy/systemd/dish-action-router.service").read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))

    assert exec_start == (
        "ExecStart=/usr/bin/caddy run --config "
        "/home/marco/ai-tools/dish/deploy/caddy/dish-action-router.json"
    )
    assert "--resume" not in exec_start


def test_router_keeps_prod_at_root_and_separates_test_authority_from_oracle() -> None:
    config = json.loads(ROUTER_CONFIG.read_text())
    routes = config["apps"]["http"]["servers"]["dish_action_router"]["routes"]

    assert routes[0]["match"] == [
        {"path": ["/test-legacy/openapi/action.json", "/test-legacy/v1/action/*"]}
    ]
    assert routes[0]["handle"][0] == {"handler": "rewrite", "strip_path_prefix": "/test-legacy"}
    assert routes[0]["handle"][1]["upstreams"] == [{"dial": "127.0.0.1:8796"}]
    assert routes[1]["match"] == [
        {"path": ["/test/openapi/action.json", "/test/v1/action/*"]}
    ]
    assert routes[1]["handle"][0] == {"handler": "rewrite", "strip_path_prefix": "/test"}
    assert routes[1]["handle"][1]["upstreams"] == [{"dial": "127.0.0.1:8766"}]
    assert routes[2].get("match") is None
    assert routes[2]["handle"][0]["upstreams"] == [{"dial": "127.0.0.1:8776"}]
    for route in routes:
        proxy = next(handle for handle in route["handle"] if handle.get("handler") == "reverse_proxy")
        assert len(proxy["upstreams"]) == 1
