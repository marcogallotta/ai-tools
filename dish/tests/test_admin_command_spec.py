"""Contracts for the shared administration command registry."""
from __future__ import annotations

import argparse

from dish_tool import admin
from dish_service import admin_cli
from dish_tool.admin_command_spec import (
    ADMIN_COMMANDS,
    ADMIN_COMMAND_SPECS,
    LEASE_FREE_ADMIN_COMMANDS,
    OPERATION_SCOPED_ADMIN_COMMANDS,
    RESOLVED_OPERATION_TARGET_COMMANDS,
    RUN_ID_ADMIN_COMMANDS,
)


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_registry_supplies_shared_command_identity_to_cli() -> None:
    assert _subcommand_names(admin_cli.build_parser()) == set(ADMIN_COMMANDS)
    assert admin_cli._ADMIN_COMMANDS is ADMIN_COMMANDS
    assert admin_cli._OPERATION_ADMIN_COMMANDS is RESOLVED_OPERATION_TARGET_COMMANDS
    assert all(name == spec.name for name, spec in ADMIN_COMMAND_SPECS.items())


def test_registry_derives_runtime_classifications() -> None:
    assert admin._OPERATION_TARGET_COMMANDS == set(RESOLVED_OPERATION_TARGET_COMMANDS)
    assert RUN_ID_ADMIN_COMMANDS <= OPERATION_SCOPED_ADMIN_COMMANDS
    assert RESOLVED_OPERATION_TARGET_COMMANDS - OPERATION_SCOPED_ADMIN_COMMANDS == {
        "recover-lease"
    }
    assert {
        "inspect",
        "abandon-operation",
        "reconcile-abandonment",
        "authorize-governed-change",
    } <= LEASE_FREE_ADMIN_COMMANDS
