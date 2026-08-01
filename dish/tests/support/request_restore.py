from __future__ import annotations


"""Shared helpers extracted from test_request_replay_and_restore_durability.py."""


import pytest

from dish_service.application import DishService

from dish_service.config import ServiceConfig

from dish_service.leases import ServicePrincipal

from tests.support.planning_intent import confirmed_planning_start

from dish_service.request_replay import begin_request

from dish_tool.commands import DishApplication

from dish_tool.database import initialize_database

from dish_tool.database_schema import MIGRATIONS, _execute_script_statements
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend as WorkflowBackend



class Backend(WorkflowBackend):
    def __init__(self):
        super().__init__(created_task_gid="1000000000000001")


def _service(tmp_path, backend=None):
    backend = backend or Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
    )
    return service, backend


class SimulatedSigkill(BaseException):
    pass


def principal(run="run"):
    return ServicePrincipal(owner_id="action", run_id=run)


def restore_source(service):
    initialize_database(service.config.db_path).close()
    source = service.backup_manager.create(label="sigkill-source")
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            "UPDATE schema_migrations SET applied_at='2999-01-01T00:00:00Z' "
            "WHERE version=(SELECT MAX(version) FROM schema_migrations)"
        )
    finally:
        conn.close()
    return source


def restart_service(service, backend):
    return DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )
