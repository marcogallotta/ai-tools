"""Approved Stage A PostgreSQL command semantic contract.

This is executable metadata for the isolated target. It mirrors §4 of
``database-backend-imp.md`` and deliberately contains no transport-owned
workflow rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


COMMAND_DEFINITIONS = {
    row.name: row
    for row in (
        CommandDefinition("create", "L", "agent", True, False, False, action_exposed=True),
        CommandDefinition("sections", "Q", "reader", False, False, False, action_exposed=True),
        CommandDefinition("section-tasks", "Q", "reader", False, False, False, action_exposed=True),
        CommandDefinition("read", "Q", "reader", False, True, False, action_exposed=True),
        CommandDefinition("inspect", "E", "verification", True, True, True, action_exposed=True),
        CommandDefinition("start", "L", "agent", True, True, False, action_exposed=True),
        CommandDefinition("prepare", "L", "agent", True, True, True, action_exposed=True),
        CommandDefinition("approve", "L", "verification", True, True, True, action_exposed=True),
        CommandDefinition("reject", "L", "verification", True, True, True, action_exposed=True),
        CommandDefinition("submit", "L", "agent", True, True, True, action_exposed=True),
        CommandDefinition("renew-lease", "L", "agent", True, True, True, action_exposed=True),
        CommandDefinition("recover", "P", "admin", True, True, False),
        CommandDefinition("repair-destination", "P", "admin", True, True, False),
        CommandDefinition("discard", "R", "admin", True, True, True),
        CommandDefinition("abandon-operation", "R", "admin", True, True, True),
        CommandDefinition("reconcile-abandonment", "R", "admin", True, True, True),
        CommandDefinition("reopen-planning", "L", "admin", True, True, False),
        CommandDefinition("reopen", "R", "admin", True, True, True),
        CommandDefinition("supply-evidence", "R", "admin", True, True, True),
        CommandDefinition("record-human-decision", "R", "admin", True, True, True),
        CommandDefinition("authorize-governed-change", "L", "admin", True, True, False),
        CommandDefinition("recover-lease", "R", "admin", True, True, False),
        CommandDefinition("expire-lease", "L", "admin", True, True, False),
        CommandDefinition("migrate", "L", "admin", True, True, False),
        CommandDefinition("settle-planning-intent", "L", "admin", True, True, False),
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
