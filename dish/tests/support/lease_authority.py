from __future__ import annotations


"""Shared helpers extracted from test_dish_tool_r51_lease_authority.py."""


from dish_service.application import DishService

from dish_service.config import ServiceConfig

from dish_service.leases import LeaseManager, ServicePrincipal

from dish_tool.database import initialize_database
from tests.support.service_foundation import _release_loader
from tests.support.service_leases import Clock
from tests.support.verification import Backend, TASK




def _principal(owner: str, run: str) -> ServicePrincipal:
    return ServicePrincipal(owner_id=owner, run_id=run)

def _service(tmp_path, *, clock=None, ttl=60):
    honest = tmp_path / "honest"
    honest.mkdir()
    backend = Backend()
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            lease_ttl_seconds=ttl,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
        lease_now=None if clock is None else clock.now,
    )
    return service, backend

def _start(service, principal):
    result = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
    )
    assert result["ok"]
    return result
