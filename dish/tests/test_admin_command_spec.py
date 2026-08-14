"""Contracts for the shared administration command registry."""
from __future__ import annotations

import argparse

from dish_tool import admin
from dish_service import admin_cli
from dish_tool.admin_command_spec import (
    ADMIN_COMMANDS,
    ADMIN_COMMAND_SPECS,
    COMPATIBILITY_ADMIN_COMMANDS,
    DETAIL_ADMIN_COMMANDS,
    LEASE_FREE_ADMIN_COMMANDS,
    PRIMARY_ADMIN_COMMANDS,
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


def _visible_subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return {choice.dest for choice in action._choices_actions}


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


def test_registry_presentation_tiers_match_operator_surface() -> None:
    assert PRIMARY_ADMIN_COMMANDS == {
        "queue", "inspect", "audit", "active", "kill", "kill-all", "kill-all-expired"
    }
    assert COMPATIBILITY_ADMIN_COMMANDS == {"issues", "attention", "active-leases"}
    assert PRIMARY_ADMIN_COMMANDS | DETAIL_ADMIN_COMMANDS | COMPATIBILITY_ADMIN_COMMANDS == ADMIN_COMMANDS
    assert not (PRIMARY_ADMIN_COMMANDS & DETAIL_ADMIN_COMMANDS)
    assert not (PRIMARY_ADMIN_COMMANDS & COMPATIBILITY_ADMIN_COMMANDS)
    assert _visible_subcommand_names(admin_cli.build_parser()) == PRIMARY_ADMIN_COMMANDS


def test_maintained_operator_docs_do_not_recommend_compatibility_commands() -> None:
    from pathlib import Path
    import re

    dish_root = Path(__file__).resolve().parents[1]
    for relative in ("README.md", "docs/runtime-contract.md"):
        text = (dish_root / relative).read_text()
        paragraphs = re.split(r"\n\s*\n", text)
        for paragraph in paragraphs:
            mentioned = {
                command
                for command in COMPATIBILITY_ADMIN_COMMANDS
                if re.search(rf"(?<![a-z-]){re.escape(command)}(?![a-z-])", paragraph)
            }
            if not mentioned:
                continue
            lower = paragraph.lower()
            explicit_invocation = any(
                re.search(rf"dish-admin\s+{re.escape(command)}\b", paragraph)
                for command in mentioned
            )
            normal_guidance = bool(
                re.search(r"\bstart(?:ing)?(?:\s+\w+){0,4}\s+with\b", lower)
                or "normal operator" in lower
                or "starting command" in lower
            )
            if explicit_invocation or normal_guidance:
                assert "compatibility" in lower or "old client" in lower, (
                    f"{relative} recommends compatibility command(s) {sorted(mentioned)} "
                    "without an explicit compatibility exception"
                )
