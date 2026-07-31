
"""Shared helpers extracted from test_action_surface.py."""


import json

import threading

import uuid

from http.client import HTTPConnection

from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService

from dish_service.client import DishActionClient, DishAdminServiceClient, DishServiceClient

from dish_service.config import ServiceConfig

from dish_service.http import build_server

from dish_service.identifiers import validate_identifier_fields


from dish_tool.backend import map_backend_exception

from dish_tool.errors import BackendFailure, DishRuleError

from dish_tool.models import RequestPhase

from dish_tool.results import error_envelope
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK



def _running(tmp_path, *, max_body=2 * 1024 * 1024):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            max_body_bytes=max_body,
            agent_token="cli-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return backend, server, thread, f"http://{host}:{port}"

def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

def _raw_post(url, path, *, token, body):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, response.getheader("Connection"), response.will_close, payload
    finally:
        connection.close()
