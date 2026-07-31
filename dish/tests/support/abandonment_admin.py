from __future__ import annotations


"""Shared helpers extracted from test_abandonment_admin_workflow.py."""


import socket

import uuid

import pytest

from dish_service.application import DishService

from dish_service.config import ServiceConfig

from dish_service.leases import LeaseManager, ServicePrincipal

from dish_tool.admin import DishAdminApplication

from dish_tool.admin_cli import build_parser

from dish_tool.application_service import CurrentWorkflowService

from dish_tool.database import declare_operation_step

from dish_tool.database_schema import initialize_database


from tests._workflow_builders import create_large_rejection_successor
from tests.support.abandonment import Backend, _release, _source
from tests.support.verification import TASK, make_app


def _released_actor_lease(conn, operation_id: str, *, owner="owner", run_id="dead-run"):
    lease = LeaseManager(conn).acquire(
        operation_id, ServicePrincipal(owner, run_id)
    )
    LeaseManager(conn).release(
        operation_id, None, reason="conversation permanently unavailable", admin=True
    )
    return lease
