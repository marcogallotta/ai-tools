from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dish_pg.frontend_board_query import BoardReadUnavailable
from dish_pg.frontend_detail_query import TaskDetailIneligible
from dish_service.frontend_auth import FrontendAuthFailure, FrontendPrincipal, LoginResult, SessionBootstrap
from dish_service.frontend_detail import DetailCapacityExceeded, TaskNotFound
from dish_service.frontend_security import csrf_proof, new_session_token
from dish_service.frontend_tokens import CursorInvalid, CursorStale

from .payloads import BoardState, board_payload, continuation_payload, detail_payload, empty_admin_payload

PASSWORD = "correct horse battery staple"
CSRF_SECRET = b"stage7-csrf-secret-material-0000001"


@dataclass(slots=True)
class _Session:
    token: str
    expires_at: datetime
    revoked: bool = False


class AcceptanceAuth:
    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self.next_lifetime_seconds = 604800
        self.validation_failure: str | None = None

    def login(self, *, password: str, peer: str, presented_token: str | None = None) -> LoginResult:
        del peer
        if password != PASSWORD:
            raise FrontendAuthFailure("login_invalid", "The shared password was not accepted.")
        if presented_token in self.sessions:
            self.sessions[presented_token].revoked = True
        token = new_session_token()
        expires_at = self._now() + timedelta(seconds=self.next_lifetime_seconds)
        self.sessions[token] = _Session(token=token, expires_at=expires_at)
        return LoginResult(token=token, principal=FrontendPrincipal(token[:16], expires_at, 1))

    def bootstrap(self, token: str) -> SessionBootstrap:
        principal = self.validate(token)
        remaining = max(0, min(604800, int((principal.expires_at - self._now()).total_seconds())))
        return SessionBootstrap(principal=principal, csrf_proof=csrf_proof(CSRF_SECRET, token), remaining_seconds=remaining)

    def validate(self, token: str, **_kwargs) -> FrontendPrincipal:
        if self.validation_failure:
            code = self.validation_failure
            self.validation_failure = None
            raise FrontendAuthFailure(code, "Session validation is temporarily unavailable." if code == "session_unavailable" else "The session is no longer valid.")
        session = self.sessions.get(token)
        if session is None:
            raise FrontendAuthFailure("auth_required", "Authentication is required.")
        if session.revoked:
            raise FrontendAuthFailure("session_revoked", "The session is no longer valid.")
        if session.expires_at <= self._now():
            raise FrontendAuthFailure("session_expired", "The session has expired.")
        return FrontendPrincipal(token[:16], session.expires_at, 1)

    def logout(self, token: str, *, csrf: str) -> None:
        if csrf != csrf_proof(CSRF_SECRET, token):
            raise FrontendAuthFailure("csrf_rejected", "Logout verification was rejected.")
        session = self.sessions.get(token)
        if session is None:
            raise FrontendAuthFailure("auth_required", "Authentication is required.")
        session.revoked = True

    def expire_all(self) -> None:
        for session in self.sessions.values():
            session.expires_at = self._now() - timedelta(seconds=1)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


class AcceptanceRuntime:
    def __init__(self, static_root: Path, *, origin: str) -> None:
        self.settings = SimpleNamespace(origin=origin, refresh_interval_seconds=1)
        self.browser_runtime_mode = "private-postgresql"
        self.static_root = static_root
        self.auth = AcceptanceAuth()
        self.board_state = BoardState()
        self.board_failure: str | None = None
        self.detail_failures: dict[str, str] = {}
        self.continuation_failure: str | None = None
        self.malformed_board = False
        self.malformed_details: set[str] = set()
        self.admin_payload = empty_admin_payload()
        self.board_calls = 0
        self.detail_calls: list[str] = []

    def board(self) -> dict[str, Any]:
        self.board_calls += 1
        if self.board_failure:
            failure = self.board_failure
            self.board_failure = None
            if failure == "unavailable":
                raise BoardReadUnavailable("acceptance failure")
            raise RuntimeError("acceptance internal failure")
        payload = board_payload(self.board_state)
        if self.malformed_board:
            self.malformed_board = False
            payload["unexpected"] = True
        return payload

    def continuation(self, *, section_route_id: str, cursor: str) -> dict[str, Any]:
        failure = self.continuation_failure
        self.continuation_failure = None
        if failure == "invalid":
            raise CursorInvalid("acceptance invalid cursor")
        if failure == "stale":
            raise CursorStale("acceptance stale cursor")
        if failure == "request-invalid":
            raise CursorInvalid("acceptance invalid cursor")
        try:
            return continuation_payload(self.board_state, section_route_id, cursor)
        except ValueError as exc:
            raise CursorInvalid(str(exc)) from exc

    def detail(self, *, task_route_id: str) -> dict[str, Any]:
        self.detail_calls.append(task_route_id)
        failure = self.detail_failures.pop(task_route_id, None)
        if failure == "not_found":
            raise TaskNotFound("acceptance missing task")
        if failure == "ineligible":
            raise TaskDetailIneligible("acceptance ineligible task")
        if failure == "capacity":
            raise DetailCapacityExceeded("acceptance detail capacity")
        if failure == "unavailable":
            raise BoardReadUnavailable("acceptance detail unavailable")
        card = self.board_state.card(task_route_id)
        if card is None:
            raise TaskNotFound("acceptance missing task")
        section_label = next(
            (label for section_id, label in self.board_state.sections if section_id == card.section_id),
            "Unknown section",
        )
        payload = detail_payload(card, section_label)
        if task_route_id in self.malformed_details:
            self.malformed_details.remove(task_route_id)
            payload["unexpected"] = True
        return payload

    def admin(self) -> dict[str, Any]:
        return self.admin_payload

    def openapi_document(self) -> dict[str, Any]:
        return {"openapi": "3.1.0"}
