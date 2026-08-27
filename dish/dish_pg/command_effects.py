"""Branch-sensitive command-effect specifications shared by planning and execution.

This module is deliberately persistence-free. Projection event types are an
authoritative runtime contract for every command. Mutation kinds describe the
planner's intended domain changes for every command, but are runtime-verified
only when ``verify_mutation_effects`` is true. That explicit flag prevents the
descriptive planner inventory from being mistaken for a fully observed commit
contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CommandEffectSpec:
    mutation_kinds: tuple[str, ...] = ()
    projection_event_types: tuple[str, ...] = ()
    verify_mutation_effects: bool = False


def effect_spec_for(
    command_name: str,
    arguments: Mapping[str, Any],
    *,
    verification_hold: bool = False,
    preconstruction_hold: bool = False,
    semantic_proposal_queued: bool = False,
    non_material_checkin: bool = False,
    planning_handoff: bool = False,
    placement_changed: bool = True,
) -> CommandEffectSpec:
    args = dict(arguments)
    if command_name == "create":
        return CommandEffectSpec(
            ("create_task", "activate_initial_document", "place_research_queue"),
            ("create_task",),
        )
    if (
        command_name == "start"
        and args.get("kind") == "planning"
        and not args.get("intent_challenge_id")
        and not args.get("prepared_operation_id")
    ):
        return CommandEffectSpec(("issue_planning_challenge",))
    if command_name == "start" and args.get("prepared_operation_id"):
        return CommandEffectSpec(
            ("claim_prepared_operation", "append_actor_fact", "issue_actor_lease")
        )
    if command_name == "start":
        return CommandEffectSpec(("open_operation", "append_actor_fact", "issue_actor_lease"))
    if command_name == "inspect":
        return CommandEffectSpec(("record_inspection_occurrence", "advance_operation"))
    if command_name in {"prepare", "migrate"}:
        if command_name == "prepare" and planning_handoff:
            mutations = [
                "activate_content_version",
            ]
            if placement_changed:
                mutations.append("place_research_queue")
            mutations.extend(("append_operation_step", "advance_operation"))
            projections = ["update_task_document"]
            if placement_changed:
                projections.append("move_task")
            return CommandEffectSpec(
                tuple(mutations),
                tuple(projections),
                verify_mutation_effects=True,
            )
        if command_name == "prepare" and non_material_checkin:
            return CommandEffectSpec(
                (
                    "activate_content_version",
                    "append_operation_step",
                    "advance_operation",
                ),
                ("update_task_document",),
                verify_mutation_effects=True,
            )
        mutations = (
            ("ensure_migration_operation",) if command_name == "migrate" else ()
        ) + (
            "activate_content_version",
            "place_verification_queue",
            "append_operation_step",
            "open_verification_cycle",
            "advance_operation",
        )
        return CommandEffectSpec(
            mutations,
            ("update_task_document", "move_task"),
            verify_mutation_effects=command_name == "prepare",
        )
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
                verify_mutation_effects=True,
            )
        return CommandEffectSpec(
            (
                "activate_corrected_content_version",
                "record_verification_signoff",
                "advance_operation",
            ),
            ("update_task_document",),
            verify_mutation_effects=True,
        )
    if command_name == "hold-reject":
        return CommandEffectSpec(
            ("open_evidence_hold", "advance_operation"),
            (),
            verify_mutation_effects=True,
        )
    if command_name == "reject":
        route = str(args.get("route", "large")).replace("_", "-")
        if route == "large":
            if semantic_proposal_queued:
                return CommandEffectSpec(
                    (
                        "reject_verification_cycle",
                        "open_human_review",
                        "advance_operation",
                    ),
                    (),
                    verify_mutation_effects=True,
                )
            mutations = [
                "reject_verification_cycle",
                "activate_corrected_content_version",
                "record_verification_correction",
            ]
            if not verification_hold:
                mutations.append("open_verification_cycle")
            mutations.append("advance_operation")
            return CommandEffectSpec(
                tuple(mutations),
                ("update_task_document",),
                verify_mutation_effects=True,
            )
        if route == "evidence":
            return CommandEffectSpec(
                (
                    "reject_verification_cycle",
                    "activate_corrected_content_version",
                    "open_evidence_hold",
                    "advance_operation",
                ),
                ("update_task_document",),
                verify_mutation_effects=True,
            )
        return CommandEffectSpec(
            (
                "reject_verification_cycle",
                "activate_corrected_content_version",
                "open_human_review",
                "advance_operation",
            ),
            ("update_task_document",),
            verify_mutation_effects=True,
        )
    if command_name == "apply-proposal":
        return CommandEffectSpec(
            (
                "activate_corrected_content_version",
                "record_verification_correction",
                "open_verification_cycle",
                "advance_operation",
            ),
            ("update_task_document",),
            verify_mutation_effects=True,
        )
    if command_name == "safe-reclaim":
        return CommandEffectSpec(
            ("fence_source_run", "publish_exact_successor"),
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
    if command_name == "cooked":
        return CommandEffectSpec(("set_completion",), verify_mutation_effects=True)
    if command_name == "archive":
        return CommandEffectSpec(("archive_task",), verify_mutation_effects=True)
    if command_name == "reopen-planning":
        return CommandEffectSpec(("clear_completion_for_planning",), ("set_completion",))
    if command_name == "reopen":
        return CommandEffectSpec(
            ("reset_verification_cycle", "activate_resumed_content_version"),
            ("update_task_document",),
        )
    if command_name == "resolved":
        return CommandEffectSpec(
            ("release_verification_hold", "activate_resumed_content_version"),
            ("update_task_document",),
        )
    if command_name == "supply-evidence":
        if preconstruction_hold:
            return CommandEffectSpec(("supply_hold_evidence", "advance_operation"))
        return CommandEffectSpec(
            ("supply_hold_evidence", "activate_resumed_content_version"),
            ("update_task_document",),
        )
    if command_name == "record-human-decision":
        return CommandEffectSpec(
            ("record_human_decision", "activate_resumed_content_version"),
            ("update_task_document",),
        )
    if command_name == "authorize-governed-change":
        return CommandEffectSpec(("create_marco_authorization",))
    if command_name in {"recover-lease", "expire-lease"}:
        return CommandEffectSpec(("release_exact_lease",))
    if command_name == "planning-intent-settlement":
        return CommandEffectSpec(("settle_planning_challenge",))
    return CommandEffectSpec()


def expected_projection_count(command_name: str, arguments: Mapping[str, Any]) -> int:
    return len(effect_spec_for(command_name, arguments).projection_event_types)
