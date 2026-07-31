from __future__ import annotations


"""Shared helpers extracted from test_operational_recovery.py."""


import threading

from datetime import datetime, timedelta, timezone

from dish_service.application import DishService

from dish_service.client import DishServiceClient

from dish_service.config import ServiceConfig

from dish_service.http import build_server

from dish_service.leases import ServicePrincipal

from dish_tool.constants import SCHEMA_VERSION

from dish_tool.database import initialize_database, record_command_audit_repair

from dish_tool.errors import DishRuleError

from dish_tool.results import result_envelope
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK



class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)

class UnavailableBackend(Backend):
    def list_sections(self, project_gid):
        raise DishRuleError(
            "BACKEND_REJECTED",
            "Asana health probe failed",
            rule="asana_probe_failed",
            retryable=True,
        )

def _service(tmp_path, backend=None, *, clock=None, ttl=90):
    backend = backend or Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "managed-backups",
            lease_ttl_seconds=ttl,
            agent_token="agent-secret",
            admin_token="admin-secret",
            port=0,
        ),
        backend_factory=lambda: backend,
        release_loader=_release_loader(honest),
        lease_now=None if clock is None else clock.now,
    )
    return service, backend

def _approved(service: DishService):
    constructor = ServicePrincipal("constructor", "constructor-run")
    verifier = ServicePrincipal("verifier", "verifier-run")
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial", "run_id": "constructor-run"},
        principal=constructor,
    )
    assert started["ok"]
    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=constructor,
    )
    assert prepared["ok"]
    review = service.execute_agent(
        "start",
        {"agent": "codex", "task_gid": "t", "kind": "verification", "run_id": "verifier-run", "independence_attestation": "independent"},
        principal=verifier,
    )
    assert review["ok"]
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": started["submission_id"]},
        principal=verifier,
    )
    assert inspected["ok"]
    approved = service.execute_agent(
        "approve",
        {
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "correction": "none",
            "reviewed_identity": review["data"]["reviewed_identity"],
            "semantic_review_complete": True,
            "provenance_complete": True,
            "run_id": "verifier-run",
        },
        principal=verifier,
    )
    assert approved["ok"]
    return started["submission_id"], verifier
