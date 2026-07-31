
"""Shared helpers extracted from test_service_leases.py."""


from datetime import datetime, timedelta, timezone

import threading

import pytest

from dish_service.application import DishService

from dish_service.config import ServiceConfig

from dish_service.leases import LeaseManager, ServicePrincipal

from dish_tool.commands import DishApplication

from dish_tool.database import initialize_database
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK



class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)

def _service(tmp_path, backend, *, clock=None, ttl=60):
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    return DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            lease_ttl_seconds=ttl,
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
        lease_now=None if clock is None else clock.now,
    )
