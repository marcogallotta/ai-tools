"""Immutable table DDL snapshots for released Stage A Alembic revisions.

This module is generated once from the approved ORM shape. Historical revisions
must not import live model metadata. Edit only through an explicitly reviewed new
migration and update the digest contract.
"""
from __future__ import annotations

import hashlib
import json

from alembic import op

FROZEN_TABLE_NAMES: dict[str, tuple[str, ...]] = {
    '0003_workflow_authority': ('service_runs', 'service_requests', 'service_request_outcomes', 'request_uncertainty_resolutions', 'command_executions', 'execution_claim_events', 'task_execution_fences', 'workflow_operations', 'operation_execution_fences', 'operation_steps', 'operation_actor_facts', 'service_leases', 'lease_events', 'planning_intent_challenges', 'marco_authorization_grants', 'marco_authorization_states', 'marco_authorization_events', 'verification_cycles', 'verification_inspection_occurrences', 'verification_corrections', 'verification_signoffs', 'evidence_holds', 'evidence_hold_events', 'human_review_requirements', 'human_review_decisions', 'abandonment_attempts', 'operation_succession_edges', 'governed_audit_events', 'causality_edges', 'invocation_audit_obligations', 'invocation_audit_repairs'),
    '0004_transition_projection': ('source_import_batches', 'source_import_entity_evidence', 'shadow_baselines', 'shadow_envelopes', 'shadow_deliveries', 'shadow_comparisons', 'shadow_gaps', 'projection_epochs', 'project_projection_mappings', 'section_projection_mappings', 'task_projection_mappings', 'projection_outbox_events', 'projection_attempts', 'projection_observations', 'projection_adjudications', 'projection_create_correlations', 'projection_drift_events', 'projection_reconciliation_runs', 'projection_reconciliation_items'),
    '0005_release_cutover': ('release_candidates', 'release_evidence_items', 'rehearsal_runs', 'rehearsal_checkpoints', 'legacy_writer_fences', 'release_evidence_bundles', 'cutover_approvals', 'cutover_runs', 'cutover_checkpoints', 'mutation_admission_controls'),
    '0006_final_asana_closure': ('final_asana_closures', 'final_asana_closure_invalidations', 'cutover_recertifications'),
    '0007_cutover_evidence_gates': ('runtime_release_attestations', 'projection_worker_readiness', 'first_admission_plans'),
}

FROZEN_IMMUTABLE_TABLE_NAMES: dict[str, tuple[str, ...]] = {
    '0003_workflow_authority': ('service_requests', 'service_request_outcomes', 'request_uncertainty_resolutions', 'execution_claim_events', 'task_execution_fences', 'operation_execution_fences', 'operation_steps', 'operation_actor_facts', 'lease_events', 'marco_authorization_grants', 'marco_authorization_events', 'verification_inspection_occurrences', 'verification_corrections', 'verification_signoffs', 'evidence_hold_events', 'human_review_decisions', 'operation_succession_edges', 'governed_audit_events', 'causality_edges', 'invocation_audit_repairs'),
    '0004_transition_projection': ('source_import_entity_evidence', 'shadow_envelopes', 'shadow_comparisons', 'projection_observations', 'projection_adjudications', 'projection_reconciliation_items'),
    '0005_release_cutover': ('release_evidence_items', 'rehearsal_checkpoints', 'release_evidence_bundles', 'cutover_approvals', 'cutover_checkpoints'),
    '0006_final_asana_closure': ('final_asana_closures', 'final_asana_closure_invalidations', 'cutover_recertifications'),
    '0007_cutover_evidence_gates': ('runtime_release_attestations', 'projection_worker_readiness', 'first_admission_plans'),
}

from .frozen_tables_0003 import FROZEN_CREATE_SQL_0003
from .frozen_tables_0004 import FROZEN_CREATE_SQL_0004
from .frozen_tables_0005 import FROZEN_CREATE_SQL_0005
from .frozen_tables_0006 import FROZEN_CREATE_SQL_0006
from .frozen_tables_0007 import FROZEN_CREATE_SQL_0007

FROZEN_CREATE_SQL: dict[str, dict[str, tuple[str, ...]]] = {
    '0003_workflow_authority': FROZEN_CREATE_SQL_0003,
    '0004_transition_projection': FROZEN_CREATE_SQL_0004,
    '0005_release_cutover': FROZEN_CREATE_SQL_0005,
    '0006_final_asana_closure': FROZEN_CREATE_SQL_0006,
    '0007_cutover_evidence_gates': FROZEN_CREATE_SQL_0007,
}


def frozen_revision_digest(revision: str) -> str:
    try:
        payload = {
            "tables": FROZEN_TABLE_NAMES[revision],
            "immutable_tables": FROZEN_IMMUTABLE_TABLE_NAMES[revision],
            "ddl": FROZEN_CREATE_SQL[revision],
        }
    except KeyError as exc:
        raise RuntimeError(f"unknown frozen revision {revision}") from exc
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_frozen_tables(revision: str) -> None:
    dialect = op.get_bind().dialect.name
    try:
        statements = FROZEN_CREATE_SQL[revision][dialect]
    except KeyError as exc:
        raise RuntimeError(f"no frozen DDL for {revision} on {dialect}") from exc
    for statement in statements:
        op.execute(statement)


def drop_frozen_tables(revision: str) -> None:
    for table_name in reversed(FROZEN_TABLE_NAMES[revision]):
        op.execute(f"DROP TABLE {table_name}")
