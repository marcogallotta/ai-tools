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
from dish_tool.database import confirm_task_content, content_identity, create_operation
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import OperationActors, ResolvedRelease
from dish_tool.results import result_envelope
from dish_tool.task_store import write_exact_content

from tests.support.backend_service_resilience import (
    _release,
    ScopeRaceBackend,
    RejectedWriteBackend,
    ReturnedBaselineWithAdvancedVersionBackend,
    _aba_operation,
)


def test_content_return_to_baseline_with_advanced_version_is_uncertain(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    backend = ReturnedBaselineWithAdvancedVersionBackend()
    identity, operation = _aba_operation(conn)
    try:
        with pytest.raises(BackendFailure) as caught:
            write_exact_content(
                conn, backend, operation_id=operation["operation_id"],
                task_gid="t", project_gid=COOKING_PROJECT_GID,
                expected_identity=identity.digest, expected_section_gid="rq",
                title="Changed", notes="Notes", schema_version="2",
            )
        attempt = conn.execute(
            "SELECT * FROM write_attempts WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert caught.value.code == "BACKEND_UNCERTAIN"
    assert caught.value.rule == "content_write_outcome_uncertain"
    assert attempt["outcome"] == "uncertain"
    assert attempt["expected_modified_at"] == "v0"
    assert attempt["version_reliable"] == 1

def test_movement_return_to_baseline_with_advanced_version_is_uncertain(tmp_path):
    from dish_tool.task_store import move_exact

    conn = initialize_database(tmp_path / "dish.db")
    backend = ReturnedBaselineWithAdvancedVersionBackend()
    identity, operation = _aba_operation(conn)
    try:
        with pytest.raises(BackendFailure) as caught:
            move_exact(
                conn, backend, operation_id=operation["operation_id"],
                task_gid="t", project_gid=COOKING_PROJECT_GID,
                expected_identity=identity.digest, expected_section_gid="rq",
                intended_section_gid="vq", purpose="test",
            )
        attempt = conn.execute(
            "SELECT * FROM movement_attempts WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert caught.value.code == "BACKEND_UNCERTAIN"
    assert caught.value.rule == "movement_outcome_uncertain"
    assert attempt["outcome"] == "uncertain"
    assert attempt["expected_modified_at"] == "v0"
    assert attempt["version_reliable"] == 1

def test_manual_recovery_refuses_baseline_after_version_advance(tmp_path):
    from dish_tool.step9 import _recover_content_attempt
    from dish_tool.task_store import read_complete_task

    conn = initialize_database(tmp_path / "dish.db")
    backend = ReturnedBaselineWithAdvancedVersionBackend()
    identity, operation = _aba_operation(conn)
    try:
        with pytest.raises(BackendFailure):
            write_exact_content(
                conn, backend, operation_id=operation["operation_id"],
                task_gid="t", project_gid=COOKING_PROJECT_GID,
                expected_identity=identity.digest, expected_section_gid="rq",
                title="Changed", notes="Notes", schema_version="2",
            )
        op = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?",
            (operation["operation_id"],),
        ).fetchone()
        live = read_complete_task(
            backend, task_gid="t", project_gid=COOKING_PROJECT_GID
        )
        with pytest.raises(DishRuleError) as caught:
            _recover_content_attempt(
                conn, operation_id=operation["operation_id"], op=op, live=live,
                requested_outcome="not-applied", actions=[],
            )
    finally:
        conn.close()
    assert caught.value.rule == "recovery_evidence_ambiguous"

def test_asana_modified_at_evidence_is_fail_closed_by_default(monkeypatch):
    monkeypatch.delenv("DISH_ASANA_MODIFIED_AT_RELIABLE_EFFECTS", raising=False)
    backend = AsanaBackend(api_client=object())
    assert backend._modified_at_reliable_effects == frozenset()

def test_asana_modified_at_evidence_requires_explicit_per_effect_certification(
    monkeypatch,
):
    monkeypatch.setenv(
        "DISH_ASANA_MODIFIED_AT_RELIABLE_EFFECTS", "content, completion"
    )
    backend = AsanaBackend(api_client=object())
    assert backend._modified_at_reliable_effects == frozenset(
        {"content", "completion"}
    )

def test_asana_modified_at_evidence_rejects_unknown_effect(monkeypatch):
    monkeypatch.setenv(
        "DISH_ASANA_MODIFIED_AT_RELIABLE_EFFECTS", "content,assignment"
    )
    with pytest.raises(DishRuleError) as caught:
        AsanaBackend(api_client=object())
    assert caught.value.rule == "asana_version_evidence_config_invalid"
