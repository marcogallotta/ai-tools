from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_tool.database import initialize_database
from tests.support.planning import Backend, release


TASK_GID = "123456789"
RUN_ID = "11111111-1111-4111-8111-111111111111"
FIRST_REQUEST = "22222222-2222-4222-8222-222222222222"
SECOND_REQUEST = "33333333-3333-4333-8333-333333333333"
THIRD_REQUEST = "44444444-4444-4444-8444-444444444444"


def service(tmp_path, backend=None, *, backend_factory=None):
    honest = tmp_path / "honest"
    honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text(
        "verification protocol", encoding="utf-8"
    )
    selected_backend = backend or Backend(task_gid=TASK_GID)
    return DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=backend_factory or (lambda: selected_backend),
        release_loader=lambda role=None: release(honest, role),
    ), selected_backend


def principal(owner_id="action", run_id=RUN_ID):
    return ServicePrincipal(owner_id=owner_id, run_id=run_id)


def planning_arguments(**extra):
    return {"agent": "gpt", "task_gid": TASK_GID, "kind": "planning", **extra}


def issue(service, *, arguments=None, request_id=FIRST_REQUEST, principal_value=None):
    return service.execute_agent(
        "start",
        arguments or planning_arguments(),
        principal=principal_value or principal(),
        request_id=request_id,
    )


def confirm(
    service,
    challenge,
    *,
    request_id=SECOND_REQUEST,
    principal_value=None,
    principal=None,
    intent_basis="user_requested",
    override_reason=None,
    arguments=None,
):
    payload = dict(arguments or planning_arguments())
    payload.update(
        {
            "intent_challenge_id": challenge["data"]["intent_challenge_id"],
            "intent_basis": intent_basis,
        }
    )
    if override_reason is not None:
        payload["override_reason"] = override_reason
    return service.execute_agent(
        "start",
        payload,
        principal=principal_value or principal or globals()["principal"](),
        request_id=request_id,
    )


def connect(service):
    return initialize_database(service.config.db_path)


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
