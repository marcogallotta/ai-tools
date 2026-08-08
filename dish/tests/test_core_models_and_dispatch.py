import json
import os
import socket
import sqlite3
import subprocess
import shutil
from pathlib import Path
from typing import Any

import pytest

FIXTURE_RELEASE_DIR = Path(__file__).resolve().parent / "fixtures" / "dish-version-current"
from dish_tool.backend import (
    AsanaBackend,
    close_asana_sdk_client,
    load_asana_pat,
    map_backend_exception,
)
from dish_tool.constants import (
    ASANA_REQUEST_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS,
    EXIT_STATUS_BY_CODE,
    MAX_REQUEST_LIFETIME_SECONDS,
    NONTERMINAL_STATES,
    READ_TIMEOUT_SECONDS,
    RECOVERY_QUARANTINE_SECONDS,
    RECOVERY_SAFETY_MARGIN_SECONDS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
)
from dish_tool.database import (
    record_audit,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.database_migrations import migrate_database
from dish_tool.database_migrations import _execute_script_statements
from dish_tool.database_schema import MIGRATIONS
from dish_tool.errors import BackendFailure, DishRuleError, ReleaseResolutionError
from dish_tool.models import (
    RequestPhase,
    SectionRegistry,
    agent_family,
    is_protocol_managed,
    material_change_line,
    material_editor_line,
    opposite_family,
    resolve_destination,
)
from dish_tool.recovery import (
    current_process_identity,
    process_identity_is_live,
)
from dish_tool.releases import resolve_release
from dish_tool.results import exit_status, result_envelope


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("agent", "family"),
    [("claude", "claude"), ("gpt", "gpt"), ("codex", "gpt")],
)
def test_agent_family_mapping(agent, family):
    assert agent_family(agent) == family
    assert opposite_family(family) != family


@pytest.mark.smoke
def test_unknown_agent_fails_closed():
    with pytest.raises(DishRuleError) as exc:
        agent_family("other")
    assert exc.value.code == "INVALID_ARGUMENT"
    assert exc.value.rule == "invalid_agent"


@pytest.mark.smoke
def test_task_provenance_uses_canonical_actor_names_and_separate_model_tokens():
    assert material_editor_line("gpt", "GPT-5.6 Thinking", "2026-07-27") == (
        "Custom GPT — self-reported model: GPT-5.6 Thinking, 2026-07-27"
    )
    assert material_editor_line("codex", "GPT-5.6-Codex", "2026-07-27") == (
        "Codex — self-reported model: GPT-5.6-Codex, 2026-07-27"
    )
    assert material_change_line(
        "gpt",
        "GPT-5.6 Thinking",
        "2026-07-27",
        change="adjusted the route",
        reason="the prior route was incomplete",
        materiality="Large",
    ) == (
        "2026-07-27 — Custom GPT — self-reported model: GPT-5.6 Thinking — adjusted the route — "
        "the prior route was incomplete — Large — pending-verification"
    )


@pytest.mark.smoke
def test_section_resolution_and_management_fail_closed():
    sections = [
        {"gid": "10", "name": "Research Queue"},
        {"gid": "11", "name": "Verification Queue"},
        {"gid": "12", "name": "Sourcing"},
        {"gid": "13", "name": "Reference"},
        {"gid": "14", "name": "Ready to Cook"},
    ]
    registry = SectionRegistry.from_sections(sections)

    assert is_protocol_managed("12", registry) is False
    assert is_protocol_managed("13", registry) is False
    assert is_protocol_managed("14", registry) is True
    assert is_protocol_managed(None, registry) is True
    assert resolve_destination("Ready to Cook", "14", registry).gid == "14"

    with pytest.raises(DishRuleError) as queue_destination:
        resolve_destination("Research Queue", "10", registry)
    assert queue_destination.value.rule == "destination_is_queue"
    with pytest.raises(DishRuleError) as unresolved_destination:
        resolve_destination("Ready to Cook", "999", registry)
    assert unresolved_destination.value.rule == "destination_unresolved"


@pytest.mark.smoke
def test_section_setup_rejects_missing_or_duplicate_names():
    sections = [
        {"gid": "10", "name": "Research Queue"},
        {"gid": "11", "name": "Verification Queue"},
        {"gid": "12", "name": "Sourcing"},
        {"gid": "13", "name": "Reference"},
        {"gid": "14", "name": "Reference"},
    ]
    with pytest.raises(DishRuleError) as ambiguous_section:
        SectionRegistry.from_sections(sections)
    assert ambiguous_section.value.rule == "section_ambiguous"


@pytest.mark.smoke
def test_common_result_contract_and_exit_statuses():
    success = result_envelope(command="prepare", state="ready")
    assert success == {
        "ok": True,
        "command": "prepare",
        "code": "OK",
        "task_gid": None,
        "submission_id": None,
        "state": "ready",
        "retryable": False,
        "allowed_actions": [],
        "data": {},
        "errors": [],
    }
    assert exit_status(success["code"]) == 0

    failure = result_envelope(
        command="prepare",
        ok=False,
        code="VALIDATION_FAILED",
        task_gid="t1",
        submission_id="s1",
        state="drafting",
        errors=[{"rule": "missing_label", "field": "Exemptions"}],
    )
    assert failure["retryable"] is True
    assert failure["allowed_actions"] == []
    assert exit_status(failure["code"]) == 2

    for code, expected in EXIT_STATUS_BY_CODE.items():
        assert exit_status(code) == expected


def test_result_envelope_never_derives_workflow_actions_from_state():
    result = result_envelope(command="probe", state="ready")

    assert result["state"] == "ready"
    assert result["allowed_actions"] == []

    explicit = result_envelope(
        command="probe", state="ready", allowed_actions=["submit"]
    )
    assert explicit["allowed_actions"] == ["submit"]


@pytest.mark.smoke
def test_agent_dispatcher_rejects_undeclared_argument_as_invalid_argument(tmp_path):
    from dish_tool.commands import DishApplication
    from dish_tool.models import ResolvedRelease

    class Backend:
        def list_sections(self, project_gid):
            raise AssertionError("handler must not run")

    release = ResolvedRelease(
        version="1.0.10", commit="", root=tmp_path, protocols={},
        schema_version="2", schema={}, schema_text="{}",
        migration_metadata={}, requested_protocol_role=None,
    )
    app = DishApplication(
        initialize_database(tmp_path / "agent.db"),
        Backend(),
        release_loader=lambda role=None: release,
    )
    result = app.execute("sections", agent="gpt", undeclared=True)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {"rule": "argument_unexpected", "field": "undeclared"}
    ]


@pytest.mark.smoke
def test_admin_dispatcher_rejects_undeclared_argument_as_invalid_argument(tmp_path):
    from dish_tool.admin import DishAdminApplication

    app = DishAdminApplication(initialize_database(tmp_path / "admin.db"))
    result = app.execute("migrate", task_gid="123", undeclared=True)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {"rule": "argument_unexpected", "field": "undeclared"}
    ]
