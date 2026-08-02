"""Authoritative command-effect specifications shared by planning and execution.

This module is deliberately persistence-free.  It is the single source for the
mutation classes a command is expected to commit and the projection outbox
event types it must create.  Handlers remain responsible for applying those
changes; the command port verifies their committed effects against this spec.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CommandEffectSpec:
    mutation_kinds: tuple[str, ...] = ()
    projection_event_types: tuple[str, ...] = ()


def effect_spec_for(command_name: str, arguments: Mapping[str, Any]) -> CommandEffectSpec:
    args = dict(arguments)
    if command_name == "create":
        return CommandEffectSpec(
            ("create_task", "activate_initial_document", "place_research_queue"),
            ("create_task",),
        )
    if command_name == "start" and args.get("kind") == "planning" and not args.get(
        "intent_challenge_id"
    ):
        return CommandEffectSpec(("issue_planning_challenge",))
    if command_name == "start":
        return CommandEffectSpec(("open_operation", "append_actor_fact", "issue_actor_lease"))
    if command_name == "inspect":
        return CommandEffectSpec(("record_inspection_occurrence", "advance_operation"))
    if command_name in {"prepare", "migrate"}:
        mutations = (
            ("ensure_migration_operation",) if command_name == "migrate" else ()
        ) + (
            "activate_content_version",
            "place_verification_queue",
            "append_operation_step",
            "open_verification_cycle",
            "advance_operation",
        )
        return CommandEffectSpec(mutations, ("update_task_document", "move_task"))
    if command_name == "approve":
        correction = str(args.get("correction", "none"))
        if correction == "small":
            return CommandEffectSpec(
                (
                    "activate_corrected_content_version",
                    "record_verification_correction",
                    "record_verification_signoff",
                    "advance_operation",
                ),
                ("update_task_document",),
            )
        return CommandEffectSpec(("record_verification_signoff", "advance_operation"))
    if command_name == "reject":
        route = str(args.get("route", "large")).replace("_", "-")
        if route == "large":
            return CommandEffectSpec(
                (
                    "reject_verification_cycle",
                    "activate_corrected_content_version",
                    "record_verification_correction",
                    "open_verification_cycle",
                    "advance_operation",
                ),
                ("update_task_document",),
            )
        if route == "evidence":
            return CommandEffectSpec(
                ("reject_verification_cycle", "open_evidence_hold", "advance_operation")
            )
        return CommandEffectSpec(
            ("reject_verification_cycle", "open_human_review", "advance_operation")
        )
    if command_name == "submit":
        return CommandEffectSpec(
            ("commit_logical_destination", "complete_operation"), ("move_task",)
        )
    if command_name == "renew-lease":
        return CommandEffectSpec(("renew_actor_lease",))
    if command_name in {"recover", "repair-destination"}:
        return CommandEffectSpec(("settle_projection_attempt",))
    if command_name == "discard":
        return CommandEffectSpec(("cancel_provably_unapplied_operation",))
    if command_name == "abandon-operation":
        return CommandEffectSpec(("begin_abandonment",))
    if command_name == "reconcile-abandonment":
        return CommandEffectSpec(("reconcile_abandonment",))
    if command_name == "reopen-planning":
        return CommandEffectSpec(("clear_completion_for_planning",), ("set_completion",))
    if command_name == "reopen":
        return CommandEffectSpec(("reset_verification_cycle",))
    if command_name == "supply-evidence":
        return CommandEffectSpec(("supply_hold_evidence",))
    if command_name == "record-human-decision":
        return CommandEffectSpec(("record_human_decision",))
    if command_name == "authorize-governed-change":
        return CommandEffectSpec(("create_marco_authorization",))
    if command_name in {"recover-lease", "expire-lease"}:
        return CommandEffectSpec(("release_exact_lease",))
    if command_name == "settle-planning-intent":
        return CommandEffectSpec(("settle_planning_challenge",))
    return CommandEffectSpec()


def expected_projection_count(command_name: str, arguments: Mapping[str, Any]) -> int:
    return len(effect_spec_for(command_name, arguments).projection_event_types)
