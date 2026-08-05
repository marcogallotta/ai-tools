from __future__ import annotations
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from dish_service import __main__ as service_main
from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import confirm_task_content, content_identity, create_operation, initialize_database
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import OperationActors, ResolvedRelease
from dish_tool.results import result_envelope
from dish_tool.task_store import write_exact_content

def _release(role=None, include_migrations=False):
    del include_migrations
    return ResolvedRelease(
        version="1.0.10",
        commit="",
        root=Path("."),
        protocols={} if role is None else {role: f"{role} protocol"},
        manifests={},
        manifest_texts={},
        schema_version="2",
        schema={},
        schema_text="{}",
        migration_metadata={},
        requested_protocol_role=role,
    )

class ScopeRaceBackend:
    def __init__(self):
        self.reads = 0

    def read_task(self, task_gid):
        self.reads += 1
        in_project = self.reads == 1
        return {
            "gid": task_gid,
            "name": "Bare",
            "notes": "",
            "completed": False,
            "modified_at": "now",
            "projects": [{"gid": COOKING_PROJECT_GID}] if in_project else [],
            "memberships": (
                [{"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": "rq"}}]
                if in_project
                else []
            ),
        }

    def list_sections(self, _project_gid):
        return [
            {"gid": "rq", "name": "Research Queue"},
            {"gid": "vq", "name": "Verification Queue"},
        ]

class RejectedWriteBackend:
    def __init__(self):
        self.title = "Title"
        self.notes = "Notes"

    def read_task(self, task_gid):
        return {
            "gid": task_gid,
            "name": self.title,
            "notes": self.notes,
            "completed": False,
            "modified_at": "now",
            "projects": [{"gid": COOKING_PROJECT_GID}],
            "memberships": [
                {"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": "rq"}}
            ],
            "_dish_version_evidence": {
                "source": "test.modified_at",
                "value": "now",
                "reliable_for": ["content"],
            },
        }

    def update_task_content(self, **_kwargs):
        raise BackendFailure(
            "BACKEND_REJECTED",
            "forbidden",
            rule="backend_access_denied",
            status=403,
            phase="response_received",
            retryable=False,
        )

class ReturnedBaselineWithAdvancedVersionBackend(RejectedWriteBackend):
    def __init__(self):
        super().__init__()
        self.modified_at = "v0"
        self.section = "rq"

    def read_task(self, task_gid):
        task = super().read_task(task_gid)
        task["modified_at"] = self.modified_at
        task["memberships"][0]["section"]["gid"] = self.section
        task["_dish_version_evidence"] = {
            "source": "test.modified_at",
            "value": self.modified_at,
            "reliable_for": ["content", "movement"],
        }
        return task

    def update_task_content(self, **_kwargs):
        self.modified_at = "v1"

    def move_task_to_section(self, **_kwargs):
        self.modified_at = "v1"

def _aba_operation(conn):
    identity = confirm_task_content(
        conn, task_gid="t", title="Title", notes="Notes",
        schema_version="2", boundary="test",
    )
    operation = create_operation(
        conn,
        task_gid="t", operation_kind="planning",
        expected_identity=identity.digest, schema_version="2",
        expected_section_gid="rq",
        actors=OperationActors(editor_agent="gpt", run_id="run"),
    )
    return identity, operation
