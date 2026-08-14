from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import ClaimError
from .service import ClaimCoordinator


class ClaimHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, coordinator: ClaimCoordinator, token: str):
        self.coordinator = coordinator
        self.service_token = token
        super().__init__(server_address, ClaimRequestHandler)


class ClaimRequestHandler(BaseHTTPRequestHandler):
    server: ClaimHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        return None

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self) -> bool:
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.server.service_token}")

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ClaimError("INVALID_REQUEST", "invalid Content-Length", 400) from exc
        if length <= 0 or length > 1_000_000:
            raise ClaimError("INVALID_REQUEST", "JSON body is required and must be <= 1 MB", 400)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClaimError("INVALID_REQUEST", "request body must be valid UTF-8 JSON", 400) from exc
        if not isinstance(payload, dict):
            raise ClaimError("INVALID_REQUEST", "request JSON must be an object", 400)
        return payload

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ClaimError):
            payload: dict[str, Any] = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            if exc.current is not None:
                payload["current"] = exc.current
            self._json(exc.status, payload)
            return
        self._json(500, {"ok": False, "error": {"code": "INTERNAL", "message": str(exc)}})

    def do_GET(self) -> None:
        if not self._auth():
            self._json(401, {"ok": False, "error": {"code": "AUTHORIZATION", "message": "invalid service token"}})
            return
        try:
            parsed = urlparse(self.path)
            if parsed.path != "/v1/claim":
                raise ClaimError("NOT_FOUND", "unknown endpoint", 404)
            task = (parse_qs(parsed.query).get("task_gid") or [None])[0]
            if task is None:
                raise ClaimError("INVALID_REQUEST", "task_gid query parameter is required", 400)
            self._json(200, {"ok": True, "claim": self.server.coordinator.status(str(task))})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        if not self._auth():
            self._json(401, {"ok": False, "error": {"code": "AUTHORIZATION", "message": "invalid service token"}})
            return
        try:
            if urlparse(self.path).path != "/v1/claim":
                raise ClaimError("NOT_FOUND", "unknown endpoint", 404)
            payload = self._body()
            action = payload.pop("action", None)
            c = self.server.coordinator
            if action == "acquire":
                result = {"claim": c.acquire(payload)}
            elif action == "takeover":
                result = {"claim": c.takeover(payload)}
            elif action == "sync":
                result = {"claim": c.sync(str(payload["task_gid"]), str(payload["claim_id"]))}
            elif action == "status":
                result = {"claim": c.status(str(payload["task_gid"]))}
            elif action == "dispatch-guard":
                result = c.dispatch_guard(str(payload["task_gid"]))
            elif action == "authorize":
                result = {"claim": c.authorize(payload)}
            elif action == "renew":
                result = {"claim": c.renew(payload)}
            elif action == "bind-branch":
                result = {"claim": c.bind_branch(payload)}
            elif action == "bind-pr":
                result = {"claim": c.bind_pr(payload)}
            elif action == "begin-publication":
                result = c.begin_publication(payload)
            elif action == "complete-publication":
                result = c.complete_publication(payload)
            elif action == "abort-publication":
                result = {"claim": c.abort_publication(payload)}
            elif action == "reconcile-publication":
                result = c.reconcile_publication(payload)
            elif action == "review-ready":
                result = {"claim": c.review_ready(payload)}
            elif action == "release":
                result = {"claim": c.release(payload)}
            elif action == "supersede":
                result = {"claim": c.supersede(payload)}
            else:
                raise ClaimError("INVALID_ACTION", f"unknown claim action {action!r}", 400)
            self._json(200, {"ok": True, **result})
        except Exception as exc:
            self._handle_error(exc)
