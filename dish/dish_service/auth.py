"""Bearer-token authentication for service surfaces."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Iterable

from dish_tool.errors import DishRuleError


@dataclass(frozen=True)
class Credential:
    client_id: str
    scope: str


def authenticate_bearer(
    authorization: str | None,
    *,
    tokens: dict[str, tuple[str, str]],
    allowed_scopes: Iterable[str],
) -> Credential:
    value = str(authorization or "")
    if value != value.strip():
        raise DishRuleError(
            "AGENT_MISMATCH",
            "service bearer token is invalid",
            rule="service_auth_invalid",
        )
    if not value.startswith("Bearer "):
        raise DishRuleError(
            "AGENT_MISMATCH",
            "service bearer authentication is required",
            rule="service_auth_required",
        )
    supplied = value[7:]
    if not supplied or supplied != supplied.strip():
        raise DishRuleError(
            "AGENT_MISMATCH", "service bearer token is invalid", rule="service_auth_invalid"
        )
    matched = None
    for token, identity in tokens.items():
        if token and hmac.compare_digest(supplied, token):
            matched = Credential(client_id=identity[0], scope=identity[1])
            break
    if matched is None:
        raise DishRuleError(
            "AGENT_MISMATCH", "service bearer token is invalid", rule="service_auth_invalid"
        )
    if matched.scope not in set(allowed_scopes):
        raise DishRuleError(
            "AGENT_MISMATCH",
            "service credential is not authorized for this surface",
            rule="service_scope_forbidden",
            details={"required_scopes": sorted(set(allowed_scopes))},
        )
    return matched
