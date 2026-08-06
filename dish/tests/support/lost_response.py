from __future__ import annotations

import http.client
import json
import uuid
from typing import Any

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from dish_tool.database import initialize_database
from tests.support.service_foundation import _release_loader
from tests.support.thread_teardown import start_server_thread


_BASE_HTTP_CONNECTION = http.client.HTTPConnection


class _LostResponse:
    def __init__(self, response: http.client.HTTPResponse) -> None:
        self._response = response
        self.status = response.status

    def read(self) -> bytes:
        self._response.read()
        raise http.client.IncompleteRead(b"", 1)


class _ReplacedResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        *,
        replacement: bytes,
        authoritative_responses: list[dict[str, Any]],
    ) -> None:
        self._response = response
        self._replacement = replacement
        self._authoritative_responses = authoritative_responses
        self.status = response.status

    def read(self) -> bytes:
        raw = self._response.read()
        self._authoritative_responses.append(json.loads(raw.decode("utf-8")))
        return self._replacement


class _CaptureFirstResponseHTTPConnection(_BASE_HTTP_CONNECTION):
    captured_payloads: list[dict[str, Any]] = []
    replace_next_response = True

    @classmethod
    def reset(cls) -> None:
        cls.captured_payloads = []
        cls.replace_next_response = True

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        if body is not None:
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.__class__.captured_payloads.append(json.loads(raw.decode("utf-8")))
        return super().request(
            method,
            url,
            body=body,
            headers=headers or {},
            encode_chunked=encode_chunked,
        )


class LoseFirstResponseHTTPConnection(_CaptureFirstResponseHTTPConnection):
    """Record request bodies and lose the first response after server completion."""

    def getresponse(self) -> http.client.HTTPResponse:
        response = super().getresponse()
        if self.__class__.replace_next_response:
            self.__class__.replace_next_response = False
            return _LostResponse(response)  # type: ignore[return-value]
        return response


class ReplaceFirstResponseHTTPConnection(_CaptureFirstResponseHTTPConnection):
    """Consume the first authoritative response, then return replacement bytes."""

    authoritative_responses: list[dict[str, Any]] = []
    replacement = b""

    @classmethod
    def reset(cls, *, replacement: bytes) -> None:
        super().reset()
        cls.authoritative_responses = []
        cls.replacement = replacement

    def getresponse(self) -> http.client.HTTPResponse:
        response = super().getresponse()
        if self.__class__.replace_next_response:
            self.__class__.replace_next_response = False
            return _ReplacedResponse(
                response,
                replacement=self.__class__.replacement,
                authoritative_responses=self.__class__.authoritative_responses,
            )  # type: ignore[return-value]
        return response


def build_inspect_ready_runtime(tmp_path, *, owner_id: str, run_id: str):
    from tests.support.verification import Backend, TASK

    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    constructor = ServicePrincipal(owner_id="constructor", run_id=str(uuid.uuid4()))
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert started["ok"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=constructor,
        request_id=str(uuid.uuid4()),
    )
    assert prepared["ok"]
    verifier = ServicePrincipal(owner_id=owner_id, run_id=run_id)
    verification = service.execute_agent(
        "start",
        {
            "agent": "codex",
            "task_gid": "t",
            "kind": "verification",
            "independence_attestation": "independent fresh run",
        },
        principal=verifier,
        request_id=str(uuid.uuid4()),
    )
    assert verification["ok"]
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="inspect-ambiguous-response")
    host, port = server.server_address
    return service, server, thread, f"http://{host}:{port}", started["submission_id"]


def inspect_fact_count(service, operation_id: str) -> int:
    conn = initialize_database(service.config.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM dish_inspect_facts WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    finally:
        conn.close()
