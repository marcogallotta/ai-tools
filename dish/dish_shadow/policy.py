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

Treatment = Literal["execute", "capture_only", "excluded"]


@dataclass(frozen=True)
class DarkLaunchTreatment:
    command_name: str
    treatment: Treatment
    reason: str
    external_effects_allowed: bool = False


_ROWS = (
    # Queries do not create governed effects and are not parity evidence for
    # the mutation dark launch.
    DarkLaunchTreatment("sections", "excluded", "read-only query"),
    DarkLaunchTreatment("section-tasks", "excluded", "read-only query"),
    DarkLaunchTreatment("read", "excluded", "read-only query"),
    DarkLaunchTreatment("proposals", "excluded", "read-only source semantic-proposal queue"),
    DarkLaunchTreatment("attention", "excluded", "read-only administrative query"),
    DarkLaunchTreatment("holds", "excluded", "read-only administrative query"),
    DarkLaunchTreatment("review-queue", "excluded", "read-only source semantic-proposal queue"),
    DarkLaunchTreatment("review-inspect", "excluded", "read-only source semantic-proposal detail"),
    # Create remains capture-only until exact lost-response correlation is
    # proved for the production Asana topology.
    DarkLaunchTreatment("create", "capture_only", "pre-cutover create correlation is not qualified"),
    DarkLaunchTreatment("apply-proposal", "capture_only", "target semantic-proposal authority is not implemented"),
    DarkLaunchTreatment("review-approve", "capture_only", "target semantic-proposal authority is not implemented"),
    DarkLaunchTreatment("review-reject", "capture_only", "target semantic-proposal authority is not implemented"),
    # Workflow mutations whose target semantics are fully local to PostgreSQL.
    DarkLaunchTreatment("start", "execute", "target workflow mutation is shadow-safe"),
    DarkLaunchTreatment("prepare", "execute", "target document and placement intents remain internal"),
    DarkLaunchTreatment("inspect", "execute", "target verification evidence remains internal"),
    DarkLaunchTreatment("approve", "execute", "target signoff and projection intent remain internal"),
    DarkLaunchTreatment("reject", "execute", "target correction or hold authority remains internal"),
    DarkLaunchTreatment("submit", "execute", "target terminal state and projection intent remain internal"),
    DarkLaunchTreatment("renew-lease", "execute", "target lease authority is internal"),
    DarkLaunchTreatment("discard", "execute", "target cancellation authority is internal"),
    DarkLaunchTreatment("abandon-operation", "execute", "target abandonment authority is internal"),
    DarkLaunchTreatment("reconcile-abandonment", "execute", "target succession authority is internal"),
    DarkLaunchTreatment("reopen-planning", "execute", "target reopen authority is internal"),
    DarkLaunchTreatment("reopen", "execute", "target continuation authority is internal"),
    DarkLaunchTreatment("supply-evidence", "execute", "target Evidence continuation is internal"),
    DarkLaunchTreatment("record-human-decision", "execute", "target Human Review decision is internal"),
    DarkLaunchTreatment("resolved", "execute", "target Verification-hold continuation is internal"),
    DarkLaunchTreatment("authorize-governed-change", "execute", "target authorization authority is internal"),
    DarkLaunchTreatment("recover-lease", "execute", "target lease recovery is internal"),
    DarkLaunchTreatment("expire-lease", "execute", "target lease expiry is internal"),
    DarkLaunchTreatment("migrate", "execute", "target migration command is internal"),
    DarkLaunchTreatment("planning-intent-settlement", "execute", "target Planning challenge settlement is internal"),
    # These routes adjudicate downstream projection attempts. The legacy and
    # target attempt identities cannot be assumed equivalent during shadowing.
    DarkLaunchTreatment("recover", "capture_only", "projection-attempt identity is target-specific"),
    DarkLaunchTreatment("repair-destination", "capture_only", "projection-attempt identity is target-specific"),
    # Retired post-cutover commands remain visible as explicit exclusions.
    DarkLaunchTreatment("backup-create", "excluded", "retired from PostgreSQL command authority"),
    DarkLaunchTreatment("backup-restore", "excluded", "retired from PostgreSQL command authority"),
)

DARK_LAUNCH_TREATMENTS = {row.command_name: row for row in _ROWS}


def treatment_for(command_name: str) -> DarkLaunchTreatment:
    try:
        return DARK_LAUNCH_TREATMENTS[command_name]
    except KeyError as exc:
        raise ValueError(f"dark-launch treatment is not registered: {command_name}") from exc
