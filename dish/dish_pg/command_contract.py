"""Approved Stage A PostgreSQL command semantic contract.

This is executable metadata for the isolated target. It follows the command/surface architecture
and deliberately contains no transport-owned workflow rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dish_service.command_spec import (
    APPROVE_COMMAND,
    CREATE_COMMAND,
    INSPECT_COMMAND,
    PREPARE_COMMAND,
    READ_COMMAND,
    REJECT_COMMAND,
    RENEW_LEASE_COMMAND,
    SECTIONS_COMMAND,
    SECTION_TASKS_COMMAND,
    START_COMMAND,
    SUBMIT_COMMAND,
    ActionCommandSpec,
)

Profile = Literal["Q", "E", "L", "R", "P", "X"]
Principal = Literal["reader", "agent", "verification", "admin", "historical"]


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    profile: Profile
    principal: Principal
    request_replay: bool
    task_required: bool
    operation_required: bool
    retained: bool = True
    action_exposed: bool = False
    description: str = ""
    workflow_action: str | None = None


def _current_action(
    current: ActionCommandSpec,
    profile: Profile,
    *,
    task_required: bool,
    operation_required: bool,
) -> CommandDefinition:
    """Project current Action identity/replay policy into the PG target metadata."""

    return CommandDefinition(
        name=current.name,
        profile=profile,
        principal=current.principal,
        request_replay=current.request_id_required,
        task_required=task_required,
        operation_required=operation_required,
        action_exposed=True,
        workflow_action=current.workflow_action,
    )


COMMAND_DEFINITIONS = {
    row.name: row
    for row in (
        _current_action(CREATE_COMMAND, "L", task_required=False, operation_required=False),
        _current_action(SECTIONS_COMMAND, "Q", task_required=False, operation_required=False),
        _current_action(SECTION_TASKS_COMMAND, "Q", task_required=False, operation_required=False),
        _current_action(READ_COMMAND, "Q", task_required=True, operation_required=False),
        CommandDefinition("attention", "Q", "admin", False, False, False),
        CommandDefinition("holds", "Q", "admin", False, False, False),
        _current_action(INSPECT_COMMAND, "E", task_required=True, operation_required=True),
        _current_action(START_COMMAND, "L", task_required=True, operation_required=False),
        _current_action(PREPARE_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(APPROVE_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(REJECT_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(SUBMIT_COMMAND, "L", task_required=True, operation_required=True),
        _current_action(RENEW_LEASE_COMMAND, "L", task_required=True, operation_required=True),
        CommandDefinition("recover", "P", "admin", True, True, False),
        CommandDefinition("repair-destination", "P", "admin", True, True, False),
        CommandDefinition("discard", "R", "admin", True, True, True),
        CommandDefinition("abandon-operation", "R", "admin", True, True, True),
        CommandDefinition("reconcile-abandonment", "R", "admin", True, True, True),
        CommandDefinition("reopen-planning", "L", "admin", True, True, False),
        CommandDefinition("reopen", "R", "admin", True, True, True, workflow_action="reopen"),
        CommandDefinition("supply-evidence", "R", "admin", True, True, True, workflow_action="supply-evidence"),
        CommandDefinition("record-human-decision", "R", "admin", True, True, True, workflow_action="record-human-decision"),
        CommandDefinition("resolved", "R", "admin", True, True, True, workflow_action="resolved"),
        CommandDefinition("authorize-governed-change", "L", "admin", True, True, False),
        CommandDefinition("revise-section-registry", "L", "admin", True, False, False),
        CommandDefinition("recover-lease", "R", "admin", True, True, False),
        CommandDefinition("expire-lease", "L", "admin", True, True, False),
        CommandDefinition("migrate", "L", "admin", True, True, False),
        CommandDefinition("planning-intent-settlement", "L", "admin", True, True, False),
        CommandDefinition("backup-create", "X", "historical", False, False, False, retained=False),
        CommandDefinition("backup-restore", "X", "historical", False, False, False, retained=False),
    )
}

ACTION_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if definition.action_exposed
)
ADMIN_COMMANDS = tuple(
    name
    for name, definition in COMMAND_DEFINITIONS.items()
    if definition.principal == "admin" or definition.principal == "historical"
)
RETAINED_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if definition.retained
)
RETIRED_COMMANDS = tuple(
    name for name, definition in COMMAND_DEFINITIONS.items() if not definition.retained
)


def definition_for(command_name: str) -> CommandDefinition:
    try:
        return COMMAND_DEFINITIONS[command_name]
    except KeyError as exc:
        raise ValueError(f"unknown PostgreSQL command: {command_name}") from exc
