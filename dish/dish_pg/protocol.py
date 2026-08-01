"""Scoped bearer protocol adapter for the PostgreSQL command port."""
from __future__ import annotations

import hmac
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Mapping

from .command_contract import definition_for
from .command_port import CommandCall, CommandPortError, CommandResult, PostgresCommandPort


class AuthenticationError(PermissionError):
    pass


class ProtocolError(ValueError):
    pass


class ScopedBearerAuthenticator:
    """Reuse the established route-class bearer model; authenticate before parsing."""

    def __init__(self, *, action_token: str, private_token: str) -> None:
        if not action_token or not private_token or action_token == private_token:
            raise ValueError("distinct non-empty Action and private bearer tokens are required")
        self.action_token = action_token
        self.private_token = private_token

    def authenticate(self, authorization: str | None, *, required_scope: str) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer credential")
        supplied = authorization.removeprefix("Bearer ")
        expected = self.action_token if required_scope == "action" else self.private_token
        if not hmac.compare_digest(supplied, expected):
            raise AuthenticationError("invalid bearer credential for route scope")


class PostgresProtocolService:
    """Thin service adapter; body loading occurs only after credential validation."""

    def __init__(
        self,
        port: PostgresCommandPort,
        authenticator: ScopedBearerAuthenticator,
    ) -> None:
        self.port = port
        self.authenticator = authenticator

    def handle(
        self,
        *,
        command_name: str,
        authorization: str | None,
        body_loader: Callable[[], Mapping[str, Any]],
        owner_id: str,
        now: datetime,
        route_scope: str = "action",
    ) -> dict[str, Any]:
        definition = definition_for(command_name)
        expected_scope = "action" if definition.action_exposed else "private"
        if route_scope != expected_scope:
            raise AuthenticationError("command is not exposed on this route class")
        self.authenticator.authenticate(authorization, required_scope=expected_scope)
        body = body_loader()
        client = body.get("client")
        arguments = body.get("arguments")
        if not isinstance(client, Mapping) or not isinstance(arguments, Mapping):
            raise ProtocolError("body requires object client and arguments")
        try:
            run_id = uuid.UUID(str(client["run_id"]))
            request_id = (
                uuid.UUID(str(client["request_id"]))
                if definition.request_replay
                else None
            )
        except (KeyError, ValueError) as exc:
            raise ProtocolError("client identifiers are missing or invalid") from exc
        if not definition.request_replay and "request_id" in client:
            raise ProtocolError("read-only command does not accept request_id")
        result = self.port.execute(
            CommandCall(
                command_name=command_name,
                arguments=dict(arguments),
                owner_id=owner_id,
                principal_class=(
                    "admin"
                    if definition.principal == "admin"
                    else "verification"
                    if definition.principal == "verification"
                    else "agent"
                ),
                run_id=run_id,
                request_id=request_id,
                now=now,
            )
        )
        return asdict(result)
