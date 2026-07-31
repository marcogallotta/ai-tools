
"""Shared helpers extracted from test_service_foundations.py."""


import threading

from dataclasses import replace

from pathlib import Path

import pytest

from dish_service.application import DishService

from dish_service.client import DishServiceClient

from dish_service.config import ServiceConfig

from dish_service.http import build_server

from dish_service.leases import ServicePrincipal

from dish_tool.commands import DishApplication

from dish_tool.database import initialize_database

from dish_tool.errors import DishRuleError

from dish_tool.models import ResolvedRelease
from tests.support.verification import Backend, TASK


def _release_loader(root: Path):
    verification = "# frozen verification\n"
    (root / "dish-verification-protocol.md").write_text(verification)

    def load(role=None, include_migrations=False):
        return ResolvedRelease(
            version="1.0.10",
            commit="",
            root=root,
            protocols={} if role is None else {role: verification if role == "verification" else f"{role} protocol"},
            manifests={},
            manifest_texts={},
            schema_version="2",
            schema={},
            schema_text="{}",
            migration_metadata={},
            requested_protocol_role=role,
        )

    return load

def _service(tmp_path, backend, *, loader=None):
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    return DishService(
        ServiceConfig(db_path=tmp_path / "shared.db", honest_root=honest, port=0, agent_token="agent-token", admin_token="admin-token"),
        backend_factory=lambda: backend,
        release_loader=loader or _release_loader(honest),
    )
