"""Deterministic Stage 4 planner and projection-effect adjudicator."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions

from .command_contract import CommandDefinition, definition_for
from .command_effects import effect_spec_for


class PlanningError(ValueError):
    """The canonical command cannot be planned against this exact snapshot."""


@dataclass(frozen=True)
class AuthorityFence:
    task_revision: int
    membership_revision: int
    placement_revision: int
    completion_revision: int
    operation_revision: int | None = None
    operation_phase: str | None = None


@dataclass(frozen=True)
class AuthoritativeSnapshot:
    generation_id: str
    task_id: str | None
    fence: AuthorityFence | None
    workflow: WorkflowSnapshot | None
    task_exists: bool
    current_content_version_id: str | None = None
    current_section_id: str | None = None
    completed: bool | None = None
    active_lease_id: str | None = None
    active_lease_owner: str | None = None
    active_lease_run_id: str | None = None
    unresolved_projection_attempt_id: str | None = None
    open_hold_id: str | None = None
    open_human_requirement_id: str | None = None
    open_abandonment_id: str | None = None


@dataclass(frozen=True)
class CanonicalCommandIntent:
    command_name: str
    arguments: Mapping[str, Any]
    principal_class: str
    owner_id: str
    run_id: str | None


@dataclass(frozen=True)
class PlannedMutation:
    kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CommandPlan:
    definition: CommandDefinition
    legal: bool
    result_code: str
    fence: AuthorityFence | None
    mutations: tuple[PlannedMutation, ...] = ()
    projection_intents: tuple[Mapping[str, Any], ...] = ()
    audit_event_type: str = ""
    causality: tuple[Mapping[str, str], ...] = ()
    recovery_guidance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectObservation:
    intended_identity: str
    observed_identity: str | None
    observed_applied: bool | None
    reread_complete: bool
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class EffectAdjudication:
    outcome: str
    retry_safe: bool
    evidence: Mapping[str, Any]


def _principal_matches(definition: CommandDefinition, principal_class: str) -> bool:
    if definition.principal == "reader":
        return principal_class in {"agent", "admin", "verification", "reader"}
    if definition.principal == "agent":
        return principal_class == "agent"
    if definition.principal == "verification":
        return principal_class in {"verification", "agent"}
    if definition.principal == "admin":
        return principal_class == "admin"
    return False


def _requires_current_action(command_name: str) -> bool:
    return command_name in {
        "prepare",
        "approve",
        "reject",
        "submit",
        "reopen",
        "supply-evidence",
        "record-human-decision",
        "resolved",
    }


def plan_command(
    *,
    snapshot: AuthoritativeSnapshot,
    intent: CanonicalCommandIntent,
    pinned_now: datetime,
) -> CommandPlan:
    """Return a deterministic plan without persistence or external effects.

    Workflow legality is delegated to ``workflow_policy.legal_actions``. This
    planner never reconstructs a second action matrix.
    """

    definition = definition_for(intent.command_name)
    if not definition.retained:
        return CommandPlan(
            definition=definition,
            legal=False,
            result_code="COMMAND_RETIRED",
            fence=snapshot.fence,
            audit_event_type="retired_command_rejected",
        )
    if not _principal_matches(definition, intent.principal_class):
        return CommandPlan(
            definition=definition,
            legal=False,
            result_code="PRINCIPAL_SCOPE_MISMATCH",
            fence=snapshot.fence,
            audit_event_type="principal_scope_rejected",
        )
    if definition.task_required and not snapshot.task_exists:
        return CommandPlan(
            definition=definition,
            legal=False,
            result_code="TASK_NOT_FOUND",
            fence=snapshot.fence,
            audit_event_type="task_missing",
        )
    if definition.operation_required and snapshot.workflow is None:
        return CommandPlan(
            definition=definition,
            legal=False,
            result_code="OPEN_OPERATION_REQUIRED",
            fence=snapshot.fence,
            audit_event_type="operation_missing",
        )
    if snapshot.open_abandonment_id and intent.command_name not in {
        "reconcile-abandonment",
        "read",
        "inspect",
    }:
        return CommandPlan(
            definition=definition,
            legal=False,
            result_code="ABANDONMENT_FENCE_ACTIVE",
            fence=snapshot.fence,
            audit_event_type="abandonment_fence_rejected",
            recovery_guidance={"abandonment_id": snapshot.open_abandonment_id},
        )
    if _requires_current_action(intent.command_name):
        assert snapshot.workflow is not None
        allowed = tuple(legal_actions(snapshot.workflow))
        if intent.command_name not in allowed:
            return CommandPlan(
                definition=definition,
                legal=False,
                result_code="ACTION_NOT_LEGAL",
                fence=snapshot.fence,
                audit_event_type="workflow_action_rejected",
                recovery_guidance={"allowed_actions": allowed},
            )

    command = intent.command_name
    args = dict(intent.arguments)
    if command in {"recover", "repair-destination"} and snapshot.unresolved_projection_attempt_id is None:
        return CommandPlan(
            definition=definition,
            legal=False,
            result_code="PROJECTION_ATTEMPT_REQUIRED",
            fence=snapshot.fence,
            audit_event_type="projection_target_missing",
        )
    effects = effect_spec_for(command, args)
    mutations = tuple(
        PlannedMutation(
            kind,
            {"attempt_id": snapshot.unresolved_projection_attempt_id, "route": command}
            if kind == "settle_projection_attempt"
            else {},
        )
        for kind in effects.mutation_kinds
    )
    projections = tuple(
        {"event_type": event_type} for event_type in effects.projection_event_types
    )

    return CommandPlan(
        definition=definition,
        legal=True,
        result_code="PLANNED",
        fence=snapshot.fence,
        mutations=mutations,
        projection_intents=projections,
        audit_event_type=f"{command}_planned",
        causality=(
            {
                "cause_type": "service_request",
                "cause_id": str(intent.arguments.get("request_id", "pending")),
                "effect_type": "command_plan",
                "effect_id": command,
            },
        ),
        recovery_guidance={"planned_at": pinned_now.isoformat()},
    )


def adjudicate_effect(
    *,
    intended_identity: str,
    observation: EffectObservation,
) -> EffectAdjudication:
    """Classify an external effect solely from exact reread evidence."""

    if observation.intended_identity != intended_identity:
        raise PlanningError("effect observation does not match intended identity")
    if not observation.reread_complete or observation.observed_applied is None:
        return EffectAdjudication("uncertain", False, dict(observation.evidence))
    if observation.observed_applied and observation.observed_identity == intended_identity:
        return EffectAdjudication("confirmed", False, dict(observation.evidence))
    if not observation.observed_applied:
        return EffectAdjudication("not_applied", True, dict(observation.evidence))
    return EffectAdjudication("uncertain", False, dict(observation.evidence))
