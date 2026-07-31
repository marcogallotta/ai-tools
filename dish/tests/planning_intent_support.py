from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dish_service.leases import ServicePrincipal


def confirmed_planning_start(
    service,
    arguments: Mapping[str, Any],
    *,
    principal: ServicePrincipal,
    challenge_request_id: str,
    start_request_id: str,
    intent_basis: str = "user_requested",
    override_reason: str | None = None,
):
    first = service.execute_agent(
        "start",
        dict(arguments),
        principal=principal,
        request_id=challenge_request_id,
    )
    assert first["code"] == "CONFIRMATION_REQUIRED", first
    confirmed = {
        **dict(arguments),
        "intent_challenge_id": first["data"]["intent_challenge_id"],
        "intent_basis": intent_basis,
    }
    if override_reason is not None:
        confirmed["override_reason"] = override_reason
    return service.execute_agent(
        "start",
        confirmed,
        principal=principal,
        request_id=start_request_id,
    )
