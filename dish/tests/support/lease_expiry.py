from __future__ import annotations


"""Shared helpers extracted from test_dish_admin_expire_lease.py."""


import socket

import threading

import uuid

import pytest

from dish_service.application import DishService

from dish_service.config import ServiceConfig

from dish_service.leases import LeaseManager, ServicePrincipal

from dish_tool.database import initialize_database
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK



OWNER_RUN = "11111111-1111-4111-8111-111111111111"

ADMIN_RUN = "22222222-2222-4222-8222-222222222222"

START_REQUEST = "44444444-4444-4444-8444-444444444444"

EXPIRY_REQUEST = "55555555-5555-4555-8555-555555555555"

TASK_GID = "123456789"

def _service(tmp_path, *, backend_factory=None, release_loader=None):
    backend = Backend(task_gid=TASK_GID)
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=backend_factory or (lambda: backend),
        release_loader=release_loader or _release_loader(honest),
    )
    return service, backend

def _start(service: DishService):
    principal = ServicePrincipal("agent", OWNER_RUN)
    result = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": TASK_GID, "kind": "initial"},
        principal=principal,
        request_id=START_REQUEST,
    )
    assert result["ok"]
    return principal, result

def _admin(run_id: str = ADMIN_RUN):
    return ServicePrincipal("marco-admin", run_id)
