"""Dark-launch command eligibility and effect-safety contract.

This registry does not define workflow legality. It only says whether an
already-authorized legacy command may be replayed against the non-authoritative
PostgreSQL shadow, captured without execution, or excluded from rollout
accounting. The PostgreSQL planner remains the sole target workflow-policy
owner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dish_service.command_spec import ACTION_COMMAND_DEFINITIONS
from dish_tool.admin_command_spec import ADMIN_COMMAND_SPECS

Treatment = Literal["execute", "capture_only", "excluded"]


@dataclass(frozen=True)
class DarkLaunchTreatment:
    command_name: str
    treatment: Treatment
    reason: str
    external_effects_allowed: bool = False


_CURRENT_COMMANDS = set(ACTION_COMMAND_DEFINITIONS) | set(ADMIN_COMMAND_SPECS)


def _treatment(
    command_name: str,
    treatment: Treatment,
    reason: str,
    *,
    target_only: bool = False,
    external_effects_allowed: bool = False,
) -> DarkLaunchTreatment:
    if not target_only and command_name not in _CURRENT_COMMANDS:
        raise ValueError(f"unknown command in dark-launch treatment: {command_name}")
    return DarkLaunchTreatment(
        command_name=command_name,
        treatment=treatment,
        reason=reason,
        external_effects_allowed=external_effects_allowed,
    )


_ROWS = (
    # Queries do not create governed effects and are not parity evidence for
    # the mutation dark launch.
    _treatment("sections", "excluded", "read-only query"),
    _treatment("section-tasks", "excluded", "read-only query"),
    _treatment("read", "excluded", "read-only query"),
    _treatment("proposals", "excluded", "read-only source semantic-proposal queue"),
    _treatment("attention", "excluded", "read-only administrative query"),
    _treatment("holds", "excluded", "read-only administrative query"),
    _treatment("review-queue", "excluded", "read-only source semantic-proposal queue"),
    _treatment("review-inspect", "excluded", "read-only source semantic-proposal detail"),
    # Create remains capture-only until exact lost-response correlation is
    # proved for the production Asana topology.
    _treatment("create", "capture_only", "pre-cutover create correlation is not qualified"),
    _treatment("apply-proposal", "capture_only", "target semantic-proposal authority is not implemented"),
    _treatment("review-approve", "capture_only", "target semantic-proposal authority is not implemented"),
    _treatment("review-reject", "capture_only", "target semantic-proposal authority is not implemented"),
    # Workflow mutations whose target semantics are fully local to PostgreSQL.
    _treatment("start", "execute", "target workflow mutation is shadow-safe"),
    _treatment("prepare", "execute", "target document and placement intents remain internal"),
    _treatment("inspect", "execute", "target verification evidence remains internal"),
    _treatment("approve", "execute", "target signoff and projection intent remain internal"),
    _treatment("reject", "execute", "target correction or hold authority remains internal"),
    _treatment("submit", "execute", "target terminal state and projection intent remain internal"),
    _treatment("renew-lease", "execute", "target lease authority is internal"),
    _treatment("discard", "execute", "target cancellation authority is internal"),
    _treatment("abandon-operation", "execute", "target abandonment authority is internal"),
    _treatment("reconcile-abandonment", "execute", "target succession authority is internal"),
    _treatment("reopen-planning", "execute", "target reopen authority is internal"),
    _treatment("reopen", "execute", "target continuation authority is internal"),
    _treatment("supply-evidence", "execute", "target Evidence continuation is internal"),
    _treatment("record-human-decision", "execute", "target Human Review decision is internal"),
    _treatment("resolved", "execute", "target Verification-hold continuation is internal"),
    _treatment("authorize-governed-change", "execute", "target authorization authority is internal"),
    _treatment("recover-lease", "execute", "target lease recovery is internal"),
    _treatment("expire-lease", "execute", "target lease expiry is internal"),
    _treatment("migrate", "execute", "target migration command is internal"),
    _treatment(
        "planning-intent-settlement",
        "execute",
        "target Planning challenge settlement is internal",
        target_only=True,
    ),
    # These routes adjudicate downstream projection attempts. The legacy and
    # target attempt identities cannot be assumed equivalent during shadowing.
    _treatment("recover", "capture_only", "projection-attempt identity is target-specific"),
    _treatment("repair-destination", "capture_only", "projection-attempt identity is target-specific"),
    # Retired post-cutover commands remain visible as explicit exclusions.
    _treatment("backup-create", "excluded", "retired from PostgreSQL command authority"),
    _treatment("backup-restore", "excluded", "retired from PostgreSQL command authority"),
)

DARK_LAUNCH_TREATMENTS = {row.command_name: row for row in _ROWS}


def treatment_for(command_name: str) -> DarkLaunchTreatment:
    try:
        return DARK_LAUNCH_TREATMENTS[command_name]
    except KeyError as exc:
        raise ValueError(f"dark-launch treatment is not registered: {command_name}") from exc
