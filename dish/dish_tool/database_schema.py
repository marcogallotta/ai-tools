"""SQLite schema definitions and durable semantic-evidence validation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .constants import SUBMISSION_STATES
from .errors import DishRuleError
from .models import utc_now

from .database_schema_migrations import MIGRATIONS


def _content_digest(title: str, notes: str) -> str:
    clean_title = str(title).replace("\r\n", "\n")
    clean_notes = str(notes).replace("\r\n", "\n")
    payload = (
        len(clean_title.encode("utf-8")).to_bytes(8, "big") + clean_title.encode("utf-8")
        + len(clean_notes.encode("utf-8")).to_bytes(8, "big") + clean_notes.encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


_SEMANTIC_RECORD_SELECTORS = {
    "content_versions": "content_version_id",
    "task_content_state": "task_gid",
    "write_attempts": "attempt_id",
    "verification_cycles": "cycle_id",
    "marco_authorizations": "authorization_id",
    "dish_inspect_facts": "fact_id",
    "planning_reopen_attempts": "attempt_id",
    "planning_intent_challenges": "challenge_id",
    "backup_creations": "request_id",
    "service_requests": "request_id",
    "operations": "operation_id",
    "service_leases": "lease_id",
    "operation_execution_claims": "claim_id",
    "operation_run_revocations": "revocation_id",
    "kill_request_bindings": "request_id",
    "operation_executions": "execution_id",
    "abandonment_attempts": "abandonment_id",
    "operation_successions": "succession_id",
    "safe_reclaims": "reclaim_id",
    "verification_hold_resets": "reset_id",
}
_SEMANTIC_PROVENANCE_FIELDS = (
    "task_gid", "operation_id", "request_id", "execution_id", "command",
    "challenge_id", "created_request_id", "claimed_request_id",
    "run_id", "actor_run_id", "owner_id", "claimed_owner_id", "cycle_id", "source_cycle_id",
)
_SEMANTIC_TIMESTAMP_FIELDS = (
    "created_at", "confirmed_at", "started_at", "finished_at", "completed_at",
    "acquired_at", "renewed_at", "expires_at", "released_at", "reserved_at",
    "claimed_at", "consumed_at", "resolved_at", "process_start", "expected_modified_at",
    "confirmed_modified_at", "content_write_completed_at",
    "signoff_completed_at", "movement_completed_at", "revoked_at",
)


def _semantic_record_row(
    conn: sqlite3.Connection, record_type: str, record_id: Any
) -> sqlite3.Row | None:
    selector = _SEMANTIC_RECORD_SELECTORS.get(record_type)
    if selector is None:
        return None
    return conn.execute(
        f"SELECT * FROM {record_type} WHERE {selector}=? LIMIT 1", (record_id,)
    ).fetchone()


def _semantic_selector(
    row: sqlite3.Row | None, field: str
) -> dict[str, str] | None:
    if row is None or field not in row.keys() or row[field] in {None, ""}:
        return None
    return {field: str(row[field])}


def _semantic_relationship(
    invariant: str,
    record_type: str,
    record_id: Any,
    row: sqlite3.Row | None,
) -> dict[str, Any]:
    """Describe the exact failed predicate without exposing governed payloads."""

    same_record = {"record_type": record_type, "record_id": str(record_id)}
    relationships: dict[str, dict[str, Any]] = {
        "content_identity_mismatch": {
            "source_fields": ["title", "notes"],
            "targets": [{**same_record, "fields": ["identity"]}],
            "required_predicate": "content_digest(title, notes) == identity",
        },
        "task_content_head_binding": {
            "source_fields": [
                "last_confirmed_content_version_id", "task_gid",
                "last_confirmed_identity", "last_confirmed_title", "last_confirmed_notes",
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "last_confirmed_content_version_id"),
                "fields": ["task_gid", "identity", "title", "notes", "confirmed"],
            }],
            "required_predicate": (
                "selected content_versions row exists, is confirmed, and exactly matches "
                "the task_content_state head"
            ),
        },
        "confirmed_write_binding": {
            "source_fields": [
                "confirmed_content_version_id", "operation_id", "intended_identity", "outcome"
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "confirmed_content_version_id"),
                "fields": ["operation_id", "identity", "confirmed"],
            }],
            "required_predicate": (
                "confirmed_content_version_id selects a confirmed version for the same operation "
                "whose identity equals intended_identity"
            ),
        },
        "verification_protocol_identity": {
            "source_fields": ["protocol_text", "protocol_release"],
            "targets": [{**same_record, "fields": ["protocol_release"]}],
            "required_predicate": "sha256(protocol_text) == digest encoded by protocol_release",
        },
        "verification_cycle_sequence": {
            "source_fields": ["task_gid"],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": {"task_gid": str(record_id)},
                "fields": ["cycle_number"],
            }],
            "required_predicate": "cycle_number values are the contiguous sequence 1..max(cycle_number)",
        },
        "consumed_authorization_binding": {
            "source_fields": [
                "consumed_at", "consumed_identity", "reserved_by_operation_id", "reserved_at"
            ],
            "targets": [{**same_record, "fields": [
                "consumed_identity", "reserved_by_operation_id", "reserved_at"
            ]}],
            "required_predicate": (
                "consumed_at implies non-empty consumed_identity, reserved_by_operation_id, and reserved_at"
            ),
        },
        "hold_baseline_binding": {
            "source_fields": [
                "hold_content_version_id", "hold_identity", "hold_section_gid", "operation_id", "task_gid"
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "hold_content_version_id"),
                "fields": ["operation_id", "task_gid", "identity", "confirmed"],
            }],
            "required_predicate": (
                "hold content exists, is confirmed, and matches the cycle operation, task, and hold identity "
                "unless migration reconciliation is explicitly required"
            ),
        },
        "approved_cycle_binding": {
            "source_fields": [
                "completed_at", "signed_content_version_id", "signed_identity", "operation_id", "task_gid"
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "signed_content_version_id"),
                "fields": ["operation_id", "task_gid", "identity", "confirmed"],
            }],
            "required_predicate": (
                "approved cycle is completed and its signed version is confirmed for the same operation and task "
                "with identity equal to signed_identity"
            ),
        },
        "small_correction_lineage": {
            "source_fields": [
                "operation_id", "task_gid", "cycle_id", "correction_class",
                "reviewed_identity", "signed_content_version_id", "signed_identity",
            ],
            "targets": [
                {
                    "record_type": "write_attempts",
                    "selector_fields": [
                        "operation_id", "purpose=signoff", "outcome=confirmed",
                        "confirmed_content_version_id=signed_content_version_id",
                        "intended_identity=signed_identity", "context_json.cycle_id=cycle_id",
                        "context_json.correction_class=small",
                    ],
                    "fields": ["expected_identity"],
                },
                {
                    "record_type": "content_versions",
                    "selector_fields": [
                        "operation_id", "task_gid", "identity=signoff.expected_identity",
                        "confirmed=1",
                    ],
                    "fields": ["content_version_id", "identity"],
                },
                {
                    "record_type": "write_attempts",
                    "selector_fields": [
                        "operation_id", "purpose=content_write", "outcome=confirmed",
                        "expected_identity=reviewed_identity",
                        "intended_identity=corrected_content.identity",
                        "confirmed_content_version_id=corrected_content.content_version_id",
                    ],
                },
            ],
            "required_predicate": (
                "an approved small correction whose reviewed and signed identities differ has a matching "
                "confirmed signoff attempt, a confirmed corrected content version, and the confirmed "
                "content-write attempt that produced that version from reviewed_identity"
            ),
        },
        "dish_inspect_fact_binding": {
            "source_fields": [
                "cycle_id", "operation_id", "task_gid", "reviewed_content_version_id",
                "reviewed_identity", "verifier_agent", "run_id", "independence_attestation",
            ],
            "targets": [
                {
                    "record_type": "verification_cycles",
                    "selector": _semantic_selector(row, "cycle_id"),
                    "fields": [
                        "operation_id", "task_gid", "reviewed_content_version_id", "reviewed_identity",
                        "verifier_agent", "run_id", "independence_attestation",
                    ],
                },
                {
                    "record_type": "content_versions",
                    "selector": _semantic_selector(row, "reviewed_content_version_id"),
                    "fields": ["operation_id", "task_gid", "identity", "confirmed"],
                },
                {
                    "record_type": "operation_actor_facts",
                    "selector_fields": [
                        "operation_id", "task_gid", "role=verifier", "verifier_agent", "run_id",
                        "independence_attestation", "reviewed_identity", "cycle_id",
                    ],
                },
            ],
            "required_predicate": (
                "inspect fact exactly matches its cycle, confirmed reviewed content version, and verifier actor fact"
            ),
        },
        "planning_reopen_completion": {
            "source_fields": ["outcome", "finished_at"],
            "targets": [{**same_record, "fields": ["finished_at"]}],
            "required_predicate": "outcome=confirmed implies finished_at is present",
        },
        "planning_reopen_pending": {
            "source_fields": ["outcome", "finished_at"],
            "targets": [{**same_record, "fields": ["finished_at"]}],
            "required_predicate": "outcome=started implies finished_at is absent",
        },
        "backup_creation_request_binding": {
            "source_fields": [
                "request_id", "backup_id", "status", "sha256", "size_bytes"
            ],
            "targets": [{
                "record_type": "service_requests",
                "selector": _semantic_selector(row, "request_id"),
                "fields": [
                    "command", "status", "result_json", "resolution_result_json",
                    "completed_at", "resolved_at",
                ],
            }],
            "required_predicate": (
                "confirmed backup creation is bound to its exact backup-create request; "
                "when the authoritative request result is successful, its metadata matches exactly"
            ),
        },
        "backup_creation_result_missing": {
            "source_fields": ["request_id", "command", "status", "result_json"],
            "targets": [{
                "record_type": "backup_creations",
                "selector": _semantic_selector(row, "request_id"),
                "fields": ["backup_id", "status", "sha256", "size_bytes"],
            }],
            "required_predicate": (
                "every authoritative successful backup-create result has one confirmed "
                "backup_creations row with the same backup identity and metadata"
            ),
        },
        "change_operation_intent_binding": {
            "source_fields": ["operation_kind", "operation_id"],
            "targets": [{
                "record_type": "operation_steps",
                "selector_fields": ["operation_id", "step_name=change_intent"],
                "fields": ["intended_json", "completed_at"],
            }],
            "required_predicate": (
                "every change operation has one completed change_intent step with level small or large "
                "and a non-empty reason"
            ),
        },
        "completed_operation_state": {
            "source_fields": [
                "status", "completed_at", "phase", "terminal_outcome", "schema_version", "expected_identity"
            ],
            "targets": [{**same_record, "fields": [
                "completed_at", "phase", "terminal_outcome", "schema_version", "expected_identity"
            ]}],
            "required_predicate": (
                "status=completed implies completed_at, terminal phase, terminal_outcome, schema_version, "
                "and expected_identity are present"
            ),
        },
        "active_operation_placement_unbound": {
            "source_fields": ["status", "expected_section_gid", "migration_reconciliation_required"],
            "targets": [{**same_record, "fields": ["expected_section_gid"]}],
            "required_predicate": (
                "open or uncertain operation has expected_section_gid unless migration reconciliation is required"
            ),
        },
        "migration_reconciliation_reason_missing": {
            "source_fields": ["migration_reconciliation_required", "migration_reconciliation_reason"],
            "targets": [{**same_record, "fields": ["migration_reconciliation_reason"]}],
            "required_predicate": (
                "migration_reconciliation_required=1 implies a non-empty migration_reconciliation_reason"
            ),
        },
        "non_material_signoff_binding": {
            "source_fields": [
                "terminal_outcome", "inherited_signoff_cycle_id", "expected_identity", "task_gid"
            ],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "inherited_signoff_cycle_id"),
                "fields": ["signed_content_version_id", "signed_identity", "outcome", "completed_at"],
            }],
            "required_predicate": (
                "non-material completion inherits a completed approved cycle with confirmed signed content for "
                "the same task, directly or through confirmed non-material lineage to expected_identity"
            ),
        },
        "operation_signoff_binding": {
            "source_fields": ["operation_id", "signoff_completed_at"],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "operation_id"),
                "selector_field": "operation_id",
                "fields": ["outcome", "signed_identity", "signed_content_version_id"],
            }],
            "required_predicate": (
                "signoff_completed_at implies an approved verification cycle with signed identity and version"
            ),
        },
        "active_lease_on_incomplete_terminal_operation": {
            "source_fields": ["operation_id", "released_at"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["status", "phase", "completed_at", "terminal_outcome"],
            }, {
                "record_type": "operation_steps/write_attempts/movement_attempts",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["completed_at", "outcome"],
            }],
            "required_predicate": (
                "active cleanup-tail lease on a terminal operation requires complete terminal state, all steps "
                "completed, and no started or uncertain external-effect attempts"
            ),
        },
        "operation_execution_claim_binding": {
            "source_fields": ["operation_id", "execution_id"],
            "targets": [{
                "record_type": "operation_executions",
                "selector": _semantic_selector(row, "execution_id"),
                "fields": ["operation_id", "status", "resolved_at"],
            }],
            "required_predicate": (
                "claim execution exists, belongs to the same operation, and is started or unresolved uncertain"
            ),
        },
        "started_operation_execution_unclaimed": {
            "source_fields": ["execution_id", "status"],
            "targets": [{
                "record_type": "operation_execution_claims",
                "selector": _semantic_selector(row, "execution_id"),
                "selector_field": "execution_id",
            }],
            "required_predicate": "status=started implies exactly one execution claim exists",
        },
        "completed_operation_execution_claimed": {
            "source_fields": ["execution_id", "status", "resolved_at"],
            "targets": [{
                "record_type": "operation_execution_claims",
                "selector": _semantic_selector(row, "execution_id"),
                "selector_field": "execution_id",
            }],
            "required_predicate": (
                "only started or unresolved uncertain executions may retain an execution claim"
            ),
        },
        "operation_execution_evidence_document": {
            "source_fields": ["evidence_json"],
            "targets": [{**same_record, "fields": ["evidence_json"]}],
            "required_predicate": "evidence_json is a JSON object",
        },
        "operation_execution_evidence_binding": {
            "source_fields": ["execution_id", "operation_id", "evidence_json"],
            "targets": [{**same_record, "fields": ["execution_id", "operation_id"]}],
            "required_predicate": (
                "evidence_json.execution_id and evidence_json.operation_id equal the owning row identifiers"
            ),
        },
        "planning_intent_creation_binding": {
            "source_fields": ["challenge_id", "created_request_id", "owner_id", "run_id", "task_gid"],
            "targets": [{
                "record_type": "service_requests",
                "selector": _semantic_selector(row, "created_request_id"),
                "fields": ["command", "owner_id", "run_id", "status", "result_json"],
            }],
            "required_predicate": (
                "created request is the completed matching start request whose result returns this exact challenge"
            ),
        },
        "planning_intent_claim_binding": {
            "source_fields": ["challenge_id", "claimed_request_id", "owner_id", "run_id"],
            "targets": [{
                "record_type": "service_requests",
                "selector": _semantic_selector(row, "claimed_request_id"),
                "fields": ["command", "owner_id", "run_id", "request_id"],
            }],
            "required_predicate": (
                "claimed request is a distinct matching start request for the same authenticated owner and run"
            ),
        },
        "planning_intent_operation_binding": {
            "source_fields": ["challenge_id", "operation_id", "task_gid", "run_id"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["operation_kind", "task_gid", "run_id"],
            }],
            "required_predicate": (
                "consumed challenge selects one Planning operation for the same task and run"
            ),
        },
        "verification_hold_reset_binding": {
            "source_fields": ["operation_id", "source_cycle_id", "candidate_identity"],
            "targets": [{
                "record_type": "content_versions",
                "selector_fields": ["operation_id", "candidate_identity"],
                "fields": ["confirmed"],
            }, {
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "source_cycle_id"),
                "fields": ["operation_id", "outcome"],
            }],
            "required_predicate": (
                "confirmed candidate content exists for the operation and source cycle belongs to the same "
                "operation with outcome=verification-hold"
            ),
        },
        "audit_operation_execution_binding": {
            "source_fields": ["operation_id", "operation_execution_id"],
            "targets": [{
                "record_type": "operation_executions",
                "selector": _semantic_selector(row, "operation_execution_id"),
                "fields": ["operation_id", "status", "resolved_at"],
            }],
            "required_predicate": (
                "operation_execution_id selects an execution owned by the same operation as the audit event"
            ),
        },
        "abandonment_attempt_authority_binding": {
            "source_fields": [
                "source_operation_id", "source_lease_id", "task_gid",
                "abandoned_owner_id", "abandoned_run_id", "attempt_cycle_id",
            ],
            "targets": [{
                "record_type": "service_leases",
                "selector": _semantic_selector(row, "source_lease_id"),
                "fields": [
                    "operation_id", "task_gid", "owner_id", "run_id",
                    "lease_kind", "actor_attempt_seq", "context_cycle_id",
                ],
            }],
            "required_predicate": (
                "the source actor lease exists and exactly matches the abandoned operation, task, owner, run, "
                "and attempt cycle"
            ),
        },
        "abandonment_attempt_cycle_binding": {
            "source_fields": ["attempt_cycle_id", "source_operation_id", "task_gid", "abandoned_run_id"],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "attempt_cycle_id"),
                "fields": ["operation_id", "task_gid", "run_id"],
            }],
            "required_predicate": (
                "attempt_cycle_id selects a cycle for the same source operation, task, and abandoned run"
            ),
        },
        "abandonment_succession_binding": {
            "source_fields": [
                "abandonment_id", "source_operation_id", "successor_operation_id", "successor_cycle_id"
            ],
            "targets": [{
                "record_type": "operation_successions",
                "selector_fields": ["abandonment_id"],
                "fields": ["source_operation_id", "successor_operation_id", "successor_cycle_id"],
            }],
            "required_predicate": (
                "the succession for this abandonment exactly matches its source operation, successor operation, "
                "and successor cycle"
            ),
        },
        "abandonment_unexpected_succession": {
            "source_fields": ["abandonment_id", "successor_operation_id"],
            "targets": [{
                "record_type": "operation_successions",
                "selector_fields": ["abandonment_id"],
                "fields": ["succession_id", "successor_operation_id"],
            }],
            "required_predicate": (
                "no succession exists while the abandonment has no successor_operation_id"
            ),
        },
        "abandonment_prepared_successor_missing": {
            "source_fields": ["abandonment_id", "status", "successor_operation_id"],
            "targets": [{
                "record_type": "operation_successions",
                "selector_fields": ["abandonment_id"],
                "fields": ["succession_id", "successor_operation_id"],
            }],
            "required_predicate": (
                "status=awaiting_successor_claim implies one durable operation succession exists"
            ),
        },
        "abandonment_execution_binding": {
            "source_fields": ["current_execution_id", "source_operation_id"],
            "targets": [{
                "record_type": "operation_executions",
                "selector": _semantic_selector(row, "current_execution_id"),
                "fields": ["operation_id", "status", "resolved_at"],
            }],
            "required_predicate": (
                "current_execution_id selects an execution owned by the abandoned source operation"
            ),
        },
        "operation_succession_binding": {
            "source_fields": [
                "succession_id", "task_gid", "source_operation_id", "successor_operation_id",
                "abandonment_id", "source_content_version_id", "successor_content_version_id",
            ],
            "targets": [
                {
                    "record_type": "operations",
                    "selector_fields": ["source_operation_id", "successor_operation_id"],
                    "fields": ["task_gid", "status", "terminal_outcome", "expected_identity"],
                },
                {
                    "record_type": "abandonment_attempts",
                    "selector": _semantic_selector(row, "abandonment_id"),
                    "fields": ["source_operation_id", "successor_operation_id"],
                },
                {
                    "record_type": "content_versions",
                    "selector_fields": ["source_content_version_id", "successor_content_version_id"],
                    "fields": ["operation_id", "task_gid", "identity", "title", "notes", "boundary", "confirmed"],
                },
            ],
            "required_predicate": (
                "source, successor, abandonment, and confirmed content versions form one exact immutable "
                "agent-abandonment succession chain"
            ),
        },
        "agent_abandoned_source_terminal_binding": {
            "source_fields": ["operation_id", "status", "terminal_outcome"],
            "targets": [
                {
                    "record_type": "operation_successions",
                    "selector_fields": ["source_operation_id=operation_id"],
                    "fields": ["succession_id", "successor_operation_id"],
                },
                {
                    "record_type": "operation_steps/write_attempts/movement_attempts",
                    "selector": _semantic_selector(row, "operation_id"),
                    "fields": ["completed_at", "outcome"],
                },
            ],
            "required_predicate": (
                "an agent-abandoned terminal operation has a succession, no incomplete steps, and no started or "
                "uncertain external-effect attempts"
            ),
        },
        "prepared_successor_authority_binding": {
            "source_fields": ["operation_id", "status", "successor_claim_mode"],
            "targets": [
                {
                    "record_type": "operation_successions",
                    "selector_fields": ["successor_operation_id=operation_id"],
                    "fields": ["succession_id", "source_operation_id"],
                },
                {
                    "record_type": "service_leases",
                    "selector": _semantic_selector(row, "operation_id"),
                    "selector_field": "operation_id",
                    "fields": ["released_at"],
                },
            ],
            "required_predicate": (
                "a prepared successor is open, has one durable succession, and has no active lease before claim"
            ),
        },
        "abandoned_verification_cycle_binding": {
            "source_fields": [
                "cycle_id", "operation_id", "outcome", "completed_at",
                "signed_content_version_id", "signed_identity",
            ],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["status", "terminal_outcome"],
            }],
            "required_predicate": (
                "an abandoned verification cycle is completed, has no signed identity/version, and belongs to "
                "an agent-abandoned cancelled operation"
            ),
        },
        "safe_reclaim_binding": {
            "source_fields": [
                "reclaim_id", "task_gid", "source_operation_id", "successor_operation_id",
                "source_lease_id", "request_id", "previous_owner_id", "previous_run_id",
                "requested_owner_id", "requested_run_id", "source_content_version_id",
                "successor_content_version_id",
            ],
            "targets": [
                {
                    "record_type": "operations",
                    "selector_fields": ["source_operation_id", "successor_operation_id"],
                    "fields": ["task_gid", "status", "terminal_outcome"],
                },
                {
                    "record_type": "service_leases",
                    "selector": _semantic_selector(row, "source_lease_id"),
                    "fields": ["operation_id", "owner_id", "run_id"],
                },
                {
                    "record_type": "service_requests",
                    "selector": _semantic_selector(row, "request_id"),
                    "fields": ["command", "owner_id", "run_id"],
                },
                {
                    "record_type": "content_versions",
                    "selector_fields": ["source_content_version_id", "successor_content_version_id"],
                    "fields": ["operation_id", "task_gid", "identity", "title", "notes", "boundary", "confirmed"],
                },
            ],
            "required_predicate": (
                "safe reclaim binds one cancelled source, its exact inactive lease/request authority, "
                "one successor, and byte-equivalent confirmed source/successor content baselines"
            ),
        },
        "safe_reclaim_prepared_successor_binding": {
            "source_fields": ["successor_operation_id", "stage", "status"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "successor_operation_id"),
                "fields": ["successor_claim_mode", "status"],
            }],
            "required_predicate": (
                "a prepared safe-reclaim successor advertises verifier claim mode for Verification "
                "or stage_actor claim mode for Planning/Research"
            ),
        },
        "safe_reclaim_claimed_successor_binding": {
            "source_fields": ["successor_operation_id", "status", "claimed_at"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "successor_operation_id"),
                "fields": ["successor_claim_mode"],
            }],
            "required_predicate": "a claimed safe-reclaim successor has consumed its prepared claim mode",
        },
        "operation_run_revocation_binding": {
            "source_fields": [
                "operation_id", "owner_id", "run_id", "source_lease_id",
            ],
            "targets": [
                {
                    "record_type": "operations",
                    "selector": _semantic_selector(row, "operation_id"),
                    "fields": ["operation_id"],
                },
                {
                    "record_type": "service_leases",
                    "selector": _semantic_selector(row, "source_lease_id"),
                    "fields": ["operation_id", "owner_id", "run_id"],
                },
            ],
            "required_predicate": (
                "operation exists and source_lease_id, when present, selects a lease for "
                "the same operation, owner, and run"
            ),
        },
        "safe_reclaim_cycle_binding": {
            "source_fields": [
                "stage", "source_operation_id", "successor_operation_id",
                "source_cycle_id", "successor_cycle_id",
            ],
            "targets": [{
                "record_type": "verification_cycles",
                "selector_fields": ["source_cycle_id", "successor_cycle_id"],
                "fields": ["operation_id", "outcome", "completed_at"],
            }],
            "required_predicate": (
                "Verification safe reclaim closes the exact source cycle as safe_reclaimed and creates "
                "a successor cycle bound to the successor operation"
            ),
        },
        "safe_reclaimed_source_terminal_binding": {
            "source_fields": ["operation_id", "status", "terminal_outcome"],
            "targets": [
                {
                    "record_type": "safe_reclaims",
                    "selector_fields": ["source_operation_id=operation_id"],
                    "fields": ["reclaim_id", "successor_operation_id"],
                },
                {
                    "record_type": "operation_steps/write_attempts/movement_attempts",
                    "selector": _semantic_selector(row, "operation_id"),
                    "fields": ["completed_at", "outcome"],
                },
            ],
            "required_predicate": (
                "a safe-reclaimed source has one reclaim lineage, no incomplete workflow step, and no "
                "started or uncertain external-effect attempt"
            ),
        },
        "safe_reclaimed_verification_cycle_binding": {
            "source_fields": [
                "cycle_id", "operation_id", "outcome", "completed_at",
                "signed_content_version_id", "signed_identity",
            ],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["status", "terminal_outcome"],
            }],
            "required_predicate": (
                "a safe-reclaimed Verification cycle is completed, unsigned, and belongs to the exact "
                "safe-reclaimed cancelled source operation"
            ),
        },
    }
    if invariant.startswith("multiple_unresolved_"):
        target = invariant.removeprefix("multiple_unresolved_")
        return {
            "source_fields": ["operation_id"],
            "targets": [{
                "record_type": target,
                "selector": {"operation_id": str(record_id)},
                "fields": ["outcome"],
            }],
            "required_predicate": "at most one row per operation has outcome started or uncertain",
        }
    return relationships.get(invariant, {
        "source_fields": [],
        "targets": [same_record],
        "required_predicate": invariant,
    })


def _semantic_problem(
    conn: sqlite3.Connection,
    invariant: str,
    record_type: str,
    record_id: Any,
    *,
    related_record_type: str | None = None,
    related_record_id: Any | None = None,
    observed_count: int | None = None,
) -> dict[str, Any]:
    """Build a payload-safe diagnostic with exact relationship and provenance."""

    row = _semantic_record_row(conn, record_type, record_id)
    problem: dict[str, Any] = {
        "invariant": invariant,
        "record_type": record_type,
        "record_id": str(record_id),
        "broken_relationship": _semantic_relationship(
            invariant, record_type, record_id, row
        ),
    }
    if row is None and record_type == "task_verification_cycles":
        problem["mutation_provenance"] = {"task_gid": str(record_id)}
    if row is not None:
        provenance = {
            field: row[field]
            for field in _SEMANTIC_PROVENANCE_FIELDS
            if field in row.keys() and row[field] not in {None, ""}
        }
        timestamps = {
            field: row[field]
            for field in _SEMANTIC_TIMESTAMP_FIELDS
            if field in row.keys() and row[field] not in {None, ""}
        }
        if provenance:
            problem["mutation_provenance"] = provenance
        if timestamps:
            problem["timestamps"] = timestamps
    if related_record_type is not None:
        problem["related_record_type"] = related_record_type
        problem["related_record_id"] = str(related_record_id)
    if observed_count is not None:
        problem["observed_count"] = int(observed_count)
    return problem


def _validate_content_and_cycle_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM content_versions WHERE confirmed=1"):
        if _content_digest(row["title"], row["notes"]) != row["identity"]:
            problems.append(_semantic_problem(conn,
                "content_identity_mismatch", "content_versions", row["content_version_id"],
            ))
    for row in conn.execute("SELECT * FROM task_content_state"):
        bound = conn.execute(
            """SELECT task_gid, identity, title, notes, confirmed
                 FROM content_versions WHERE content_version_id=?""",
            (row["last_confirmed_content_version_id"],),
        ).fetchone()
        if (
            bound is None
            or bound["confirmed"] != 1
            or bound["task_gid"] != row["task_gid"]
            or bound["identity"] != row["last_confirmed_identity"]
            or bound["title"] != row["last_confirmed_title"]
            or bound["notes"] != row["last_confirmed_notes"]
        ):
            problems.append(_semantic_problem(conn,
                "task_content_head_binding", "task_content_state", row["task_gid"],
            ))
    for row in conn.execute("SELECT * FROM write_attempts WHERE outcome='confirmed'"):
        bound = conn.execute(
            "SELECT identity,confirmed,operation_id FROM content_versions WHERE content_version_id=?",
            (row["confirmed_content_version_id"],),
        ).fetchone()
        if bound is None or bound["confirmed"] != 1 or bound["operation_id"] != row["operation_id"] or bound["identity"] != row["intended_identity"]:
            problems.append(_semantic_problem(conn,
                "confirmed_write_binding", "write_attempts", row["attempt_id"],
            ))
    for row in conn.execute("SELECT * FROM verification_cycles"):
        release = str(row["protocol_release"] or "")
        text = str(row["protocol_text"] or "")
        if release.startswith("sha256:") and hashlib.sha256(text.encode("utf-8")).hexdigest() != release.split(":", 1)[1].split(";", 1)[0].strip():
            problems.append(_semantic_problem(conn,
                "verification_protocol_identity", "verification_cycles", row["cycle_id"],
            ))
    for task in conn.execute("SELECT DISTINCT task_gid FROM verification_cycles"):
        numbers = [r[0] for r in conn.execute("SELECT cycle_number FROM verification_cycles WHERE task_gid=? ORDER BY cycle_number", (task[0],))]
        if numbers and numbers != list(range(1, max(numbers) + 1)):
            problems.append(_semantic_problem(conn,
                "verification_cycle_sequence", "task_verification_cycles", task[0],
            ))
    for row in conn.execute("SELECT * FROM marco_authorizations WHERE consumed_at IS NOT NULL"):
        if not row["consumed_identity"] or not row["reserved_by_operation_id"] or not row["reserved_at"]:
            problems.append(_semantic_problem(conn,
                "consumed_authorization_binding", "marco_authorizations", row["authorization_id"],
            ))
    for row in conn.execute(
        """SELECT cycle.*, operation.migration_reconciliation_required
             FROM verification_cycles AS cycle
             JOIN operations AS operation ON operation.operation_id=cycle.operation_id
            WHERE cycle.completed_at IS NOT NULL
              AND (cycle.route IN ('evidence','human_review') OR cycle.outcome='verification-hold')"""
    ):
        held = conn.execute(
            "SELECT task_gid,operation_id,identity,confirmed FROM content_versions WHERE content_version_id=?",
            (row["hold_content_version_id"],),
        ).fetchone()
        valid = bool(
            row["hold_identity"] and row["hold_section_gid"] and held is not None
            and held["confirmed"] == 1 and held["task_gid"] == row["task_gid"]
            and held["operation_id"] == row["operation_id"]
            and held["identity"] == row["hold_identity"]
        )
        if not valid and row["migration_reconciliation_required"] != 1:
            problems.append(_semantic_problem(conn,
                "hold_baseline_binding", "verification_cycles", row["cycle_id"],
            ))
    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='approved'"):
        signed = conn.execute(
            "SELECT identity,confirmed,operation_id,task_gid FROM content_versions WHERE content_version_id=?",
            (row["signed_content_version_id"],),
        ).fetchone()
        if (row["completed_at"] is None or row["signed_identity"] is None or signed is None
                or signed["confirmed"] != 1 or signed["operation_id"] != row["operation_id"]
                or signed["task_gid"] != row["task_gid"] or signed["identity"] != row["signed_identity"]):
            problems.append(_semantic_problem(conn,
                "approved_cycle_binding", "verification_cycles", row["cycle_id"],
            ))
    for row in conn.execute(
        """SELECT cycle.*
             FROM verification_cycles AS cycle
             JOIN operations AS operation ON operation.operation_id=cycle.operation_id
            WHERE cycle.outcome='approved' AND cycle.correction_class='small'
              AND cycle.reviewed_identity IS NOT cycle.signed_identity
              AND operation.migration_reconciliation_required != 1"""
    ):
        signoff_attempt = None
        for attempt in conn.execute(
            """SELECT * FROM write_attempts
                 WHERE operation_id=? AND purpose='signoff' AND outcome='confirmed'
                   AND confirmed_content_version_id=? AND intended_identity=?
                 ORDER BY started_at DESC, rowid DESC""",
            (
                row["operation_id"],
                row["signed_content_version_id"],
                row["signed_identity"],
            ),
        ):
            try:
                context = json.loads(attempt["context_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if (
                isinstance(context, dict)
                and context.get("cycle_id") == row["cycle_id"]
                and context.get("correction_class") == "small"
            ):
                signoff_attempt = attempt
                break
        corrected_version = None
        correction_write = None
        if signoff_attempt is not None:
            corrected_version = conn.execute(
                """SELECT content_version_id,operation_id,task_gid,identity,confirmed
                     FROM content_versions
                    WHERE operation_id=? AND task_gid=? AND identity=? AND confirmed=1
                    ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (
                    row["operation_id"],
                    row["task_gid"],
                    signoff_attempt["expected_identity"],
                ),
            ).fetchone()
        if corrected_version is not None:
            correction_write = conn.execute(
                """SELECT attempt_id FROM write_attempts
                     WHERE operation_id=? AND purpose='content_write' AND outcome='confirmed'
                       AND expected_identity=? AND intended_identity=?
                       AND confirmed_content_version_id=?
                     ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (
                    row["operation_id"],
                    row["reviewed_identity"],
                    corrected_version["identity"],
                    corrected_version["content_version_id"],
                ),
            ).fetchone()
        if signoff_attempt is None or corrected_version is None or correction_write is None:
            problems.append(_semantic_problem(conn,
                "small_correction_lineage", "verification_cycles", row["cycle_id"],
            ))

def _validate_operation_and_inspection_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM dish_inspect_facts"):
        cycle = conn.execute(
            """SELECT operation_id,task_gid,reviewed_content_version_id,reviewed_identity,
                      verifier_agent,run_id,independence_attestation
                 FROM verification_cycles WHERE cycle_id=?""",
            (row["cycle_id"],),
        ).fetchone()
        version = conn.execute(
            "SELECT operation_id,task_gid,identity,confirmed FROM content_versions WHERE content_version_id=?",
            (row["reviewed_content_version_id"],),
        ).fetchone()
        actor = conn.execute(
            """SELECT 1 FROM operation_actor_facts
                 WHERE operation_id=? AND task_gid=? AND role='verifier'
                   AND agent=? AND run_id=?
                   AND COALESCE(independence_attestation,'')=COALESCE(?, '')
                   AND candidate_identity=? AND source_cycle_id=? LIMIT 1""",
            (row["operation_id"], row["task_gid"], row["verifier_agent"], row["run_id"],
             row["independence_attestation"], row["reviewed_identity"], row["cycle_id"]),
        ).fetchone()
        if (
            cycle is None or version is None or actor is None
            or cycle["operation_id"] != row["operation_id"]
            or cycle["task_gid"] != row["task_gid"]
            or cycle["reviewed_content_version_id"] != row["reviewed_content_version_id"]
            or cycle["reviewed_identity"] != row["reviewed_identity"]
            or cycle["verifier_agent"] != row["verifier_agent"]
            or cycle["run_id"] != row["run_id"]
            or (cycle["independence_attestation"] or "") != (row["independence_attestation"] or "")
            or version["operation_id"] != row["operation_id"]
            or version["task_gid"] != row["task_gid"]
            or version["identity"] != row["reviewed_identity"] or version["confirmed"] != 1
        ):
            problems.append(_semantic_problem(conn,
                "dish_inspect_fact_binding", "dish_inspect_facts", row["fact_id"],
            ))
    for row in conn.execute("SELECT * FROM planning_reopen_attempts"):
        if row["outcome"] == "confirmed" and not row["finished_at"]:
            problems.append(_semantic_problem(conn,
                "planning_reopen_completion", "planning_reopen_attempts", row["attempt_id"],
            ))
        if row["outcome"] == "started" and row["finished_at"] is not None:
            problems.append(_semantic_problem(conn,
                "planning_reopen_pending", "planning_reopen_attempts", row["attempt_id"],
            ))
    for row in conn.execute("SELECT * FROM operations"):
        if (
            row["operation_kind"] == "change"
            and row["migration_reconciliation_required"] != 1
        ):
            intent = conn.execute(
                """SELECT intended_json, completed_at FROM operation_steps
                     WHERE operation_id=? AND step_name='change_intent'""",
                (row["operation_id"],),
            ).fetchone()
            try:
                intended = None if intent is None else json.loads(intent["intended_json"])
            except (TypeError, ValueError):
                intended = None
            valid_intent = bool(
                intent is not None
                and intent["completed_at"]
                and isinstance(intended, dict)
                and intended.get("level") in {"small", "large"}
                and isinstance(intended.get("reason"), str)
                and intended["reason"].strip()
            )
            if not valid_intent:
                problems.append(_semantic_problem(
                    conn,
                    "change_operation_intent_binding",
                    "operations",
                    row["operation_id"],
                    related_record_type="operation_steps",
                    related_record_id=f"{row['operation_id']}:change_intent",
                ))
        if row["status"] == "completed" and (row["completed_at"] is None or row["phase"] != "terminal" or not row["terminal_outcome"] or not row["schema_version"] or not row["expected_identity"]):
            problems.append(_semantic_problem(conn,
                "completed_operation_state", "operations", row["operation_id"],
            ))
        if row["status"] in {"open", "uncertain"}:
            if row["expected_section_gid"] is None and row["migration_reconciliation_required"] != 1:
                problems.append(_semantic_problem(conn,
                    "active_operation_placement_unbound", "operations", row["operation_id"],
                ))
            if row["migration_reconciliation_required"] == 1 and not str(row["migration_reconciliation_reason"] or "").strip():
                problems.append(_semantic_problem(conn,
                    "migration_reconciliation_reason_missing", "operations", row["operation_id"],
                ))
        if row["terminal_outcome"] == "non_material_checkin":
            inherited = conn.execute(
                """SELECT cycle.signed_identity, cycle.signed_content_version_id,
                          cycle.outcome, cycle.completed_at, version.identity,
                          version.confirmed, version.task_gid
                     FROM verification_cycles AS cycle
                     LEFT JOIN content_versions AS version
                       ON version.content_version_id=cycle.signed_content_version_id
                    WHERE cycle.cycle_id=?""",
                (row["inherited_signoff_cycle_id"],),
            ).fetchone()
            lineage = conn.execute(
                """SELECT 1
                     FROM operations AS prior
                     JOIN write_attempts AS candidate_write
                       ON candidate_write.operation_id=prior.operation_id
                              AND candidate_write.outcome='confirmed'
                     JOIN content_versions AS candidate
                       ON candidate.content_version_id=candidate_write.confirmed_content_version_id
                    WHERE prior.task_gid=?
                      AND prior.status='completed'
                      AND prior.terminal_outcome='non_material_checkin'
                      AND prior.inherited_signoff_cycle_id=?
                      AND candidate_write.intended_identity=?
                      AND candidate.confirmed=1
                      AND candidate.task_gid=?
                      AND candidate.identity=?
                    LIMIT 1""",
                (
                    row["task_gid"], row["inherited_signoff_cycle_id"],
                    row["expected_identity"], row["task_gid"], row["expected_identity"],
                ),
            ).fetchone()
            if (
                inherited is None or inherited["outcome"] != "approved"
                or inherited["completed_at"] is None
                or inherited["identity"] != inherited["signed_identity"]
                or inherited["confirmed"] != 1 or inherited["task_gid"] != row["task_gid"]
                or (inherited["signed_identity"] != row["expected_identity"] and lineage is None)
            ):
                problems.append(_semantic_problem(conn,
                    "non_material_signoff_binding", "operations", row["operation_id"],
                ))
        if row["signoff_completed_at"] is not None:
            approved = conn.execute(
                "SELECT 1 FROM verification_cycles WHERE operation_id=? AND outcome='approved' AND signed_identity IS NOT NULL AND signed_content_version_id IS NOT NULL",
                (row["operation_id"],),
            ).fetchone()
            if approved is None:
                problems.append(_semantic_problem(conn,
                    "operation_signoff_binding", "operations", row["operation_id"],
                ))

def _validate_execution_and_lease_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for table in ("write_attempts", "movement_attempts"):
        for row in conn.execute(
            f"""SELECT operation_id, COUNT(*) AS unresolved_count
                  FROM {table}
                 WHERE outcome IN ('started','uncertain')
                 GROUP BY operation_id
                HAVING COUNT(*) > 1"""
        ):
            problems.append(_semantic_problem(conn,
                f"multiple_unresolved_{table}",
                "operations",
                row["operation_id"],
                related_record_type=table,
                related_record_id=row["operation_id"],
                observed_count=int(row["unresolved_count"]),
            ))
    # A service lease is transport ownership, not workflow state. Terminal
    # status revokes mutation authority before response cleanup, so a complete
    # terminal operation may retain an active cleanup-tail lease. It is safe
    # only after every declared step and external-effect attempt is resolved;
    # otherwise the terminal row still contradicts its durable evidence.
    for row in conn.execute(
        """SELECT lease.lease_id, lease.operation_id, operation.phase,
                  operation.completed_at, operation.terminal_outcome
             FROM service_leases AS lease
             JOIN operations AS operation ON operation.operation_id=lease.operation_id
            WHERE lease.released_at IS NULL
              AND operation.status IN ('completed','cancelled')"""
    ):
        pending = conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT 1 FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        terminal_incomplete = (
            row["phase"] != "terminal"
            or not row["completed_at"]
            or not row["terminal_outcome"]
        )
        if terminal_incomplete or pending is not None or unresolved is not None:
            problems.append(_semantic_problem(conn,
                "active_lease_on_incomplete_terminal_operation",
                "service_leases",
                row["lease_id"],
                related_record_type="operations",
                related_record_id=row["operation_id"],
            ))
    for row in conn.execute("SELECT * FROM operation_execution_claims"):
        if row["execution_id"] is None:
            continue
        execution = conn.execute(
            "SELECT operation_id,status,resolved_at FROM operation_executions WHERE execution_id=?",
            (row["execution_id"],),
        ).fetchone()
        if (
            execution is None
            or execution["operation_id"] != row["operation_id"]
            or not (
                execution["status"] == "started"
                or (execution["status"] == "uncertain" and execution["resolved_at"] is None)
            )
        ):
            problems.append(_semantic_problem(conn,
                "operation_execution_claim_binding",
                "operation_execution_claims",
                row["claim_id"],
            ))
    for row in conn.execute("SELECT * FROM operation_executions"):
        claim = conn.execute(
            "SELECT 1 FROM operation_execution_claims WHERE execution_id=?",
            (row["execution_id"],),
        ).fetchone()
        if row["status"] == "started" and claim is None:
            problems.append(_semantic_problem(conn,
                "started_operation_execution_unclaimed",
                "operation_executions",
                row["execution_id"],
            ))
        if (
            row["status"] not in {"started", "uncertain"}
            or (row["status"] == "uncertain" and row["resolved_at"] is not None)
        ) and claim is not None:
            problems.append(_semantic_problem(conn,
                "completed_operation_execution_claimed",
                "operation_executions",
                row["execution_id"],
            ))
        if row["evidence_json"]:
            try:
                recovery = json.loads(row["evidence_json"])
            except (TypeError, ValueError):
                problems.append(_semantic_problem(conn,
                    "operation_execution_evidence_document",
                    "operation_executions",
                    row["execution_id"],
                ))
                continue
            if not isinstance(recovery, dict):
                problems.append(_semantic_problem(conn,
                    "operation_execution_evidence_document",
                    "operation_executions",
                    row["execution_id"],
                ))
                continue
            if (
                recovery.get("execution_id") != row["execution_id"]
                or recovery.get("operation_id") != row["operation_id"]
            ):
                problems.append(_semantic_problem(conn,
                    "operation_execution_evidence_binding",
                    "operation_executions",
                    row["execution_id"],
                ))
    for row in conn.execute(
        """SELECT audit.event_id,audit.operation_id,audit.operation_execution_id,
                  execution.operation_id AS execution_operation_id
             FROM audit_events AS audit
             LEFT JOIN operation_executions AS execution
               ON execution.execution_id=audit.operation_execution_id
            WHERE audit.operation_execution_id IS NOT NULL"""
    ):
        if (
            row["execution_operation_id"] is None
            or row["operation_id"] != row["execution_operation_id"]
        ):
            problems.append(_semantic_problem(
                conn,
                "audit_operation_execution_binding",
                "audit_events",
                row["event_id"],
                related_record_type="operation_executions",
                related_record_id=row["operation_execution_id"],
            ))

def _validate_planning_intent_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM planning_intent_challenges"):
        created = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?",
            (row["created_request_id"],),
        ).fetchone()
        valid_created = False
        if (
            created is not None
            and created["command"] == "start"
            and created["owner_id"] == row["owner_id"]
            and created["run_id"] == row["run_id"]
            and created["status"] == "completed"
        ):
            try:
                result = json.loads(created["result_json"] or "null")
            except (TypeError, ValueError):
                result = None
            confirmation = (
                (result.get("data") or {}).get("planning_intent_confirmation")
                if isinstance(result, dict)
                else None
            )
            valid_created = bool(
                isinstance(result, dict)
                and result.get("code") == "CONFIRMATION_REQUIRED"
                and result.get("task_gid") == row["task_gid"]
                and (result.get("data") or {}).get("intent_challenge_id")
                == row["challenge_id"]
                and isinstance(confirmation, dict)
                and confirmation.get("challenge_id") == row["challenge_id"]
            )
        if not valid_created:
            problems.append(_semantic_problem(
                conn,
                "planning_intent_creation_binding",
                "planning_intent_challenges",
                row["challenge_id"],
                related_record_type="service_requests",
                related_record_id=row["created_request_id"],
            ))

        if row["status"] in {"claimed", "consumed"}:
            claimed = conn.execute(
                "SELECT * FROM service_requests WHERE request_id=?",
                (row["claimed_request_id"],),
            ).fetchone()
            if (
                claimed is None
                or claimed["command"] != "start"
                or claimed["owner_id"] != row["owner_id"]
                or claimed["run_id"] != row["run_id"]
                or claimed["request_id"] == row["created_request_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "planning_intent_claim_binding",
                    "planning_intent_challenges",
                    row["challenge_id"],
                    related_record_type="service_requests",
                    related_record_id=row["claimed_request_id"],
                ))

        if row["status"] == "consumed":
            operation = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?",
                (row["operation_id"],),
            ).fetchone()
            if (
                operation is None
                or operation["operation_kind"] != "planning"
                or operation["task_gid"] != row["task_gid"]
                or operation["run_id"] != row["run_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "planning_intent_operation_binding",
                    "planning_intent_challenges",
                    row["challenge_id"],
                    related_record_type="operations",
                    related_record_id=row["operation_id"],
                ))


def _validate_backup_and_reset_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    def authoritative_backup_result(request: sqlite3.Row) -> dict[str, Any] | None:
        try:
            encoded = request["resolution_result_json"] or request["result_json"]
            result = json.loads(encoded or "null")
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    for row in conn.execute("SELECT * FROM backup_creations WHERE status='confirmed'"):
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (row["request_id"],)
        ).fetchone()
        valid = bool(request is not None and request["command"] == "backup-create")
        if valid and request["status"] == "completed":
            result = authoritative_backup_result(request)
            if isinstance(result, dict) and result.get("ok"):
                backup = (result.get("data") or {}).get("backup")
                valid = bool(
                    isinstance(backup, dict)
                    and backup.get("backup_id") == row["backup_id"]
                    and backup.get("sha256") == row["sha256"]
                    and backup.get("size_bytes") == row["size_bytes"]
                )
            # A completed failure with exact confirmed destination evidence is a
            # supported crash/reconciliation frontier. Startup or exact replay
            # may add the successful resolution result without replacing the
            # original first outcome.
        if not valid:
            problems.append(_semantic_problem(
                conn,
                "backup_creation_request_binding",
                "backup_creations",
                row["request_id"],
            ))
    for request in conn.execute(
        "SELECT * FROM service_requests WHERE command='backup-create' AND status='completed'"
    ):
        result = authoritative_backup_result(request)
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        backup = (result.get("data") or {}).get("backup")
        creation = conn.execute(
            "SELECT * FROM backup_creations WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()
        if not (
            creation is not None
            and creation["status"] == "confirmed"
            and isinstance(backup, dict)
            and backup.get("backup_id") == creation["backup_id"]
            and backup.get("sha256") == creation["sha256"]
            and backup.get("size_bytes") == creation["size_bytes"]
        ):
            problems.append(_semantic_problem(
                conn,
                "backup_creation_result_missing",
                "service_requests",
                request["request_id"],
            ))
    for row in conn.execute("SELECT * FROM verification_hold_resets"):
        version = conn.execute(
            """SELECT 1 FROM content_versions
                 WHERE operation_id=? AND identity=? AND confirmed=1 LIMIT 1""",
            (row["operation_id"], row["candidate_identity"]),
        ).fetchone()
        cycle = conn.execute(
            "SELECT 1 FROM verification_cycles WHERE cycle_id=? AND operation_id=? AND outcome='verification-hold'",
            (row["source_cycle_id"], row["operation_id"]),
        ).fetchone()
        if version is None or cycle is None:
            problems.append(_semantic_problem(conn,
                "verification_hold_reset_binding", "verification_hold_resets", row["reset_id"],
            ))

def _validate_operation_run_revocation_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM operation_run_revocations"):
        operation = conn.execute(
            "SELECT 1 FROM operations WHERE operation_id=?", (row["operation_id"],)
        ).fetchone()
        lease = None
        if row["source_lease_id"] is not None:
            lease = conn.execute(
                "SELECT operation_id,owner_id,run_id FROM service_leases WHERE lease_id=?",
                (row["source_lease_id"],),
            ).fetchone()
        if operation is None or (
            row["source_lease_id"] is not None
            and (
                lease is None
                or lease["operation_id"] != row["operation_id"]
                or lease["owner_id"] != row["owner_id"]
                or lease["run_id"] != row["run_id"]
            )
        ):
            problems.append(_semantic_problem(
                conn,
                "operation_run_revocation_binding",
                "operation_run_revocations",
                row["revocation_id"],
                related_record_type=(
                    "service_leases" if row["source_lease_id"] is not None else "operations"
                ),
                related_record_id=(row["source_lease_id"] or row["operation_id"]),
            ))


def _validate_abandonment_attempt_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM abandonment_attempts"):
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (row["source_lease_id"],)
        ).fetchone()
        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["source_operation_id"],)
        ).fetchone()
        if (
            lease is None
            or source is None
            or source["task_gid"] != row["task_gid"]
            or lease["operation_id"] != row["source_operation_id"]
            or lease["task_gid"] != row["task_gid"]
            or lease["owner_id"] != row["abandoned_owner_id"]
            or lease["run_id"] != row["abandoned_run_id"]
            or lease["lease_kind"] != "actor"
            or lease["actor_attempt_seq"] is None
            or lease["context_cycle_id"] != row["attempt_cycle_id"]
        ):
            problems.append(_semantic_problem(
                conn,
                "abandonment_attempt_authority_binding",
                "abandonment_attempts",
                row["abandonment_id"],
                related_record_type="service_leases",
                related_record_id=row["source_lease_id"],
            ))
        if row["attempt_cycle_id"] is not None:
            cycle = conn.execute(
                "SELECT operation_id,task_gid,run_id FROM verification_cycles WHERE cycle_id=?",
                (row["attempt_cycle_id"],),
            ).fetchone()
            if (
                cycle is None
                or cycle["operation_id"] != row["source_operation_id"]
                or cycle["task_gid"] != row["task_gid"]
                or cycle["run_id"] != row["abandoned_run_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "abandonment_attempt_cycle_binding",
                    "abandonment_attempts",
                    row["abandonment_id"],
                    related_record_type="verification_cycles",
                    related_record_id=row["attempt_cycle_id"],
                ))
        succession = conn.execute(
            "SELECT * FROM operation_successions WHERE abandonment_id=?",
            (row["abandonment_id"],),
        ).fetchone()
        if row["successor_operation_id"] is not None:
            if (
                succession is None
                or succession["source_operation_id"] != row["source_operation_id"]
                or succession["successor_operation_id"] != row["successor_operation_id"]
                or succession["successor_cycle_id"] != row["successor_cycle_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "abandonment_succession_binding",
                    "abandonment_attempts",
                    row["abandonment_id"],
                    related_record_type="operation_successions",
                    related_record_id=None if succession is None else succession["succession_id"],
                ))
        elif succession is not None:
            problems.append(_semantic_problem(
                conn,
                "abandonment_unexpected_succession",
                "abandonment_attempts",
                row["abandonment_id"],
                related_record_type="operation_successions",
                related_record_id=succession["succession_id"],
            ))
        if row["status"] == "awaiting_successor_claim" and succession is None:
            problems.append(_semantic_problem(
                conn,
                "abandonment_prepared_successor_missing",
                "abandonment_attempts",
                row["abandonment_id"],
            ))
        if row["current_execution_id"] is not None:
            execution = conn.execute(
                "SELECT operation_id FROM operation_executions WHERE execution_id=?",
                (row["current_execution_id"],),
            ).fetchone()
            if execution is None or execution["operation_id"] != row["source_operation_id"]:
                problems.append(_semantic_problem(
                    conn,
                    "abandonment_execution_binding",
                    "abandonment_attempts",
                    row["abandonment_id"],
                    related_record_type="operation_executions",
                    related_record_id=row["current_execution_id"],
                ))


def _validate_safe_reclaim_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM safe_reclaims"):
        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["source_operation_id"],)
        ).fetchone()
        successor = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["successor_operation_id"],)
        ).fetchone()
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (row["source_lease_id"],)
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (row["request_id"],)
        ).fetchone()
        source_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["source_content_version_id"],),
        ).fetchone()
        successor_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["successor_content_version_id"],),
        ).fetchone()
        invalid = (
            source is None
            or successor is None
            or lease is None
            or request is None
            or source_version is None
            or successor_version is None
            or source["task_gid"] != row["task_gid"]
            or source["status"] != "cancelled"
            or source["terminal_outcome"] != "safe_reclaimed"
            or successor["task_gid"] != row["task_gid"]
            or lease["operation_id"] != row["source_operation_id"]
            or lease["owner_id"] != row["previous_owner_id"]
            or lease["run_id"] != row["previous_run_id"]
            or request["command"] != "safe-reclaim"
            or request["owner_id"] != row["requested_owner_id"]
            or request["run_id"] != row["requested_run_id"]
            or source_version["task_gid"] != row["task_gid"]
            or source_version["confirmed"] != 1
            or successor_version["operation_id"] != row["successor_operation_id"]
            or successor_version["boundary"] != "successor_baseline"
            or successor_version["confirmed"] != 1
            or successor_version["identity"] != source_version["identity"]
            or successor_version["title"] != source_version["title"]
            or successor_version["notes"] != source_version["notes"]
        )
        if invalid:
            problems.append(_semantic_problem(
                conn, "safe_reclaim_binding", "safe_reclaims", row["reclaim_id"]
            ))
            continue
        expected_mode = "verifier" if row["stage"] == "verification" else "stage_actor"
        if row["status"] == "prepared" and successor["successor_claim_mode"] != expected_mode:
            problems.append(_semantic_problem(
                conn, "safe_reclaim_prepared_successor_binding", "safe_reclaims", row["reclaim_id"]
            ))
        if row["status"] == "claimed" and successor["successor_claim_mode"] != "none":
            problems.append(_semantic_problem(
                conn, "safe_reclaim_claimed_successor_binding", "safe_reclaims", row["reclaim_id"]
            ))
        if row["stage"] == "verification":
            source_cycle = conn.execute(
                "SELECT * FROM verification_cycles WHERE cycle_id=?", (row["source_cycle_id"],)
            ).fetchone()
            successor_cycle = conn.execute(
                "SELECT * FROM verification_cycles WHERE cycle_id=?", (row["successor_cycle_id"],)
            ).fetchone()
            if (
                source_cycle is None
                or source_cycle["operation_id"] != row["source_operation_id"]
                or source_cycle["outcome"] != "safe_reclaimed"
                or source_cycle["completed_at"] is None
                or successor_cycle is None
                or successor_cycle["operation_id"] != row["successor_operation_id"]
            ):
                problems.append(_semantic_problem(
                    conn, "safe_reclaim_cycle_binding", "safe_reclaims", row["reclaim_id"]
                ))

    for row in conn.execute(
        "SELECT operation_id FROM operations WHERE status='cancelled' AND terminal_outcome='safe_reclaimed'"
    ):
        reclaim = conn.execute(
            "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (row["operation_id"],)
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT 1 FROM write_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        if reclaim is None or pending is not None or unresolved is not None:
            problems.append(_semantic_problem(
                conn, "safe_reclaimed_source_terminal_binding", "operations", row["operation_id"]
            ))

    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='safe_reclaimed'"):
        operation = conn.execute(
            "SELECT status,terminal_outcome FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        if (
            row["completed_at"] is None
            or row["signed_content_version_id"] is not None
            or row["signed_identity"] is not None
            or operation is None
            or operation["status"] != "cancelled"
            or operation["terminal_outcome"] != "safe_reclaimed"
        ):
            problems.append(_semantic_problem(
                conn, "safe_reclaimed_verification_cycle_binding", "verification_cycles", row["cycle_id"]
            ))

def _validate_succession_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM operation_successions"):
        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["source_operation_id"],)
        ).fetchone()
        successor = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["successor_operation_id"],)
        ).fetchone()
        abandonment = conn.execute(
            "SELECT * FROM abandonment_attempts WHERE abandonment_id=?", (row["abandonment_id"],)
        ).fetchone()
        source_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["source_content_version_id"],),
        ).fetchone()
        successor_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["successor_content_version_id"],),
        ).fetchone()
        if (
            source is None
            or successor is None
            or abandonment is None
            or source_version is None
            or successor_version is None
            or source["task_gid"] != row["task_gid"]
            or source["status"] != "cancelled"
            or source["terminal_outcome"] != "agent_abandoned"
            or successor["task_gid"] != row["task_gid"]
            or successor_version["operation_id"] != successor["operation_id"]
            or successor_version["boundary"] != "successor_baseline"
            or successor_version["confirmed"] != 1
            or source_version["confirmed"] != 1
            or source_version["task_gid"] != row["task_gid"]
            or successor_version["task_gid"] != row["task_gid"]
            or successor_version["identity"] != source_version["identity"]
            or successor_version["title"] != source_version["title"]
            or successor_version["notes"] != source_version["notes"]
            or successor["expected_identity"] != successor_version["identity"]
            or abandonment["source_operation_id"] != source["operation_id"]
            or abandonment["successor_operation_id"] != successor["operation_id"]
        ):
            problems.append(_semantic_problem(
                conn,
                "operation_succession_binding",
                "operation_successions",
                row["succession_id"],
            ))

    for row in conn.execute(
        "SELECT operation_id FROM operations WHERE status='cancelled' AND terminal_outcome='agent_abandoned'"
    ):
        succession = conn.execute(
            "SELECT 1 FROM operation_successions WHERE source_operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT 1 FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        if succession is None or pending is not None or unresolved is not None:
            problems.append(_semantic_problem(
                conn,
                "agent_abandoned_source_terminal_binding",
                "operations",
                row["operation_id"],
            ))

    for row in conn.execute(
        "SELECT * FROM operations WHERE successor_claim_mode IN ('stage_actor','verifier')"
    ):
        succession = conn.execute(
            "SELECT * FROM operation_successions WHERE successor_operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        reclaim = conn.execute(
            "SELECT * FROM safe_reclaims WHERE successor_operation_id=? AND status='prepared'",
            (row["operation_id"],),
        ).fetchone()
        active_lease = conn.execute(
            "SELECT 1 FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (row["operation_id"],),
        ).fetchone()
        if (succession is None and reclaim is None) or row["status"] != "open" or active_lease is not None:
            related_type = "safe_reclaims" if reclaim is not None else "operation_successions"
            related_id = (
                reclaim["reclaim_id"] if reclaim is not None
                else (None if succession is None else succession["succession_id"])
            )
            problems.append(_semantic_problem(
                conn,
                "prepared_successor_authority_binding",
                "operations",
                row["operation_id"],
                related_record_type=related_type,
                related_record_id=related_id,
            ))

    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='abandoned'"):
        operation = conn.execute(
            "SELECT status,terminal_outcome FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        if (
            row["completed_at"] is None
            or row["signed_content_version_id"] is not None
            or row["signed_identity"] is not None
            or operation is None
            or operation["status"] != "cancelled"
            or operation["terminal_outcome"] != "agent_abandoned"
        ):
            problems.append(_semantic_problem(
                conn,
                "abandoned_verification_cycle_binding",
                "verification_cycles",
                row["cycle_id"],
            ))


def _validate_semantic_evidence(conn: sqlite3.Connection) -> None:
    problems: list[dict[str, Any]] = []
    _validate_content_and_cycle_evidence(conn, problems)
    _validate_operation_and_inspection_evidence(conn, problems)
    _validate_execution_and_lease_evidence(conn, problems)
    _validate_planning_intent_evidence(conn, problems)
    _validate_backup_and_reset_evidence(conn, problems)
    _validate_operation_run_revocation_evidence(conn, problems)
    _validate_abandonment_attempt_evidence(conn, problems)
    _validate_succession_evidence(conn, problems)
    _validate_safe_reclaim_evidence(conn, problems)
    if problems:
        raise DishRuleError(
            "VALIDATION_FAILED", "database durable evidence is semantically inconsistent",
            rule="database_semantic_evidence_invalid",
            details={
                "problems": problems[:50],
                "problem_count": len(problems),
                "diagnostic_timestamp": utc_now(),
                "transaction_state": {
                    "connection_in_transaction": bool(conn.in_transaction),
                    "evidence_visibility": (
                        "connection_local_uncommitted"
                        if conn.in_transaction
                        else "committed_database"
                    ),
                },
            },
        )
