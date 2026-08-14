"""Contracts for the shared administration command registry."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

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


NON_PRIMARY_ADMIN_COMMANDS_BY_TIER = {
    "detail": DETAIL_ADMIN_COMMANDS,
    "compatibility": COMPATIBILITY_ADMIN_COMMANDS,
}
PRESENTATION_EXCEPTION_MARKERS = {
    "detail": ("detail", "advanced", "exact next action", "exact-next-action"),
    "compatibility": ("compatibility", "old client", "old caller"),
}


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


def _is_normal_navigation_guidance(paragraph: str) -> bool:
    lower = paragraph.lower()
    return bool(
        re.search(r"\bstart(?:ing)?(?:\s+\w+){0,4}\s+with\b", lower)
        or re.search(r"\breview(?:s|ing)?(?:\s+\w+){0,4}\s+with\b", lower)
        or "normal operator" in lower
        or "starting command" in lower
        or "normal navigation" in lower
        or "global read-only inventory" in lower
    )


def _assert_operator_doc_command_presentation(text: str, relative: str) -> None:
    paragraphs = re.split(r"\n\s*\n", text)
    for index, paragraph in enumerate(paragraphs):
        lower = paragraph.lower()
        normal_guidance = _is_normal_navigation_guidance(paragraph) or (
            index > 0 and _is_normal_navigation_guidance(paragraphs[index - 1])
        )
        for tier, commands in NON_PRIMARY_ADMIN_COMMANDS_BY_TIER.items():
            mentioned = {
                command
                for command in commands
                if re.search(rf"`{re.escape(command)}`", paragraph)
                or re.search(rf"dish-admin\s+{re.escape(command)}\b", paragraph)
            }
            if not mentioned:
                continue
            explicitly_invoked = {
                command
                for command in mentioned
                if re.search(rf"dish-admin\s+{re.escape(command)}\b", paragraph)
            }
            needs_exception = normal_guidance or (
                tier == "compatibility" and bool(explicitly_invoked)
            )
            if not needs_exception:
                continue
            assert any(
                marker in lower for marker in PRESENTATION_EXCEPTION_MARKERS[tier]
            ), (
                f"{relative} recommends {tier} command(s) {sorted(mentioned)} "
                f"without an explicit {tier} exception"
            )


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
    assert (
        PRIMARY_ADMIN_COMMANDS | DETAIL_ADMIN_COMMANDS | COMPATIBILITY_ADMIN_COMMANDS
        == ADMIN_COMMANDS
    )
    assert not (PRIMARY_ADMIN_COMMANDS & DETAIL_ADMIN_COMMANDS)
    assert not (PRIMARY_ADMIN_COMMANDS & COMPATIBILITY_ADMIN_COMMANDS)
    assert _visible_subcommand_names(admin_cli.build_parser()) == PRIMARY_ADMIN_COMMANDS


def test_maintained_operator_docs_do_not_recommend_non_primary_commands() -> None:
    dish_root = Path(__file__).resolve().parents[1]
    for relative in ("README.md", "docs/runtime-contract.md"):
        _assert_operator_doc_command_presentation(
            (dish_root / relative).read_text(), relative
        )


def test_operator_doc_drift_gate_rejects_review_queue_as_normal_start() -> None:
    with pytest.raises(AssertionError, match="review-queue"):
        _assert_operator_doc_command_presentation(
            "Start with `dish-admin review-queue` to review pending work.",
            "synthetic.md",
        )


def test_operator_doc_drift_gate_rejects_review_queue_navigation_code_block() -> None:
    with pytest.raises(AssertionError, match="review-queue"):
        _assert_operator_doc_command_presentation(
            "Marco reviews the queue with:\n\n```sh\ndish-admin review-queue\n```",
            "synthetic.md",
        )


def test_operator_doc_drift_gate_allows_explicit_review_queue_detail_guidance() -> None:
    _assert_operator_doc_command_presentation(
        "The normal operator entry point is `dish-admin queue`. "
        "The hidden `review-queue` command remains a detail view.",
        "synthetic.md",
    )
