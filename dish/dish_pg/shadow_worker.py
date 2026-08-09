"""Dark-launch legacy-spool delivery and PostgreSQL shadow execution worker.

This worker is separate from projection_worker and reconciliation_worker. It
never calls Asana. PostgreSQL command execution may create transactional
projection intents tagged with origin ``shadow``. Projection workers reject
those rows unconditionally, independent of epoch effect configuration. The
shared filesystem kill switch stops both new legacy capture and this worker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Mapping, Protocol

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from dish_service.path_safety import require_distinct_paths
from dish_service.shadow_spool import ShadowSpool, ShadowSpoolItem
from dish_shadow.policy import treatment_for
from dish_tool.constants import RECOVERY_QUARANTINE_SECONDS

from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from .command_port import CommandCall, CommandResult, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .read_model import ReadModelError
from .shadow_evidence import (
    ShadowEvaluation,
    canonical_legacy_state,
    canonical_transition,
)
from .transition import ShadowService, TransitionAuthorityError
from .workflow import WorkflowAuthorityService

LOGGER = logging.getLogger("dish.shadow_worker")
_NAMESPACE = uuid.UUID("b40de1df-43e5-445c-9eed-c87f17d2b526")
_IDENTIFIER_ROLES = {
    "submission_id": ("operation_id", "operation"),
    "operation_id": ("operation_id", "operation"),
    "existing_submission_id": ("operation_id", "operation"),
    "target_operation_id": ("operation_id", "operation"),
    "prepared_operation_id": ("successor_operation_id", "operation"),
    "successor_operation_id": ("successor_operation_id", "operation"),
    "lease_id": ("lease_id", "lease"),
    "cycle_id": ("cycle_id", "verification_cycle"),
    "target_cycle_id": ("cycle_id", "verification_cycle"),
    "expected_cycle_id": ("cycle_id", "verification_cycle"),
    "verification_cycle_id": ("cycle_id", "verification_cycle"),
    "new_cycle_id": ("cycle_id", "verification_cycle"),
    "prepared_cycle_id": ("prepared_cycle_id", "verification_cycle"),
    "intent_challenge_id": ("challenge_id", "planning_challenge"),
    "challenge_id": ("challenge_id", "planning_challenge"),
    "attempt_id": ("abandonment_id", "abandonment"),
    "abandonment_id": ("abandonment_id", "abandonment"),
    "requirement_id": ("requirement_id", "human_review_requirement"),
    "hold_id": ("hold_id", "evidence_hold"),
    "grant_id": ("grant_id", "authorization_grant"),
}



class ShadowIdentityMappingError(ValueError):
    """A captured source authority identifier has no unique target binding."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _shadow_uuid(envelope: tx.ShadowEnvelope, *, label: str, value: Any) -> uuid.UUID:
    """Namespace source identities so shadow rows cannot collide with live authority."""
    return uuid.uuid5(
        _NAMESPACE,
        ":".join(
            (
                label,
                str(envelope.shadow_baseline_id),
                str(envelope.source_authority_generation or "unknown-source-generation"),
                str(value),
            )
        ),
    )


def _shadow_agent(arguments: Mapping[str, Any], owner_id: str) -> str:
    allowed = {"claude", "gpt", "codex", "marco", "service"}
    candidates = (arguments.get("agent"), owner_id.rsplit(":", 1)[-1])
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in allowed:
            return normalized
    return "service"


def _ensure_shadow_run(
    session,
    *,
    envelope: tx.ShadowEnvelope,
    arguments: Mapping[str, Any],
    owner_id: str,
    source_run_identity: Any,
    target_run_id: uuid.UUID,
    generation_id: uuid.UUID,
) -> wf.ServiceRun:
    agent = _shadow_agent(arguments, owner_id)
    capability_digest = hashlib.sha256(
        "\0".join(
            (
                "dish-shadow-run-v1",
                str(envelope.shadow_baseline_id),
                str(envelope.source_authority_generation or ""),
                owner_id,
                str(source_run_identity),
                agent,
            )
        ).encode("utf-8")
    ).digest()
    existing = session.get(wf.ServiceRun, target_run_id)
    if existing is not None:
        if (
            existing.generation_id != generation_id
            or existing.owner_id != owner_id
            or existing.agent != agent
            or existing.capability_digest != capability_digest
            or existing.status != "active"
        ):
            raise ValueError("shadow run identity conflicts with existing target authority")
        return existing
    return WorkflowAuthorityService(session).register_run(
        run_id=target_run_id,
        generation_id=generation_id,
        owner_id=owner_id,
        agent=agent,
        capability_digest=capability_digest,
        registered_at=envelope.captured_at,
    )


def _imported_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    family: str,
    source_value: str,
) -> str | None:
    """Resolve preserved legacy IDs only from this baseline's exact import lineage."""
    try:
        identifier = uuid.UUID(source_value)
    except (TypeError, ValueError):
        return None
    baseline = session.get(tx.ShadowBaseline, envelope.shadow_baseline_id)
    if baseline is None:
        return None
    model = {
        "operation": wf.WorkflowOperation,
        "lease": wf.ServiceLease,
        "verification_cycle": wf.VerificationCycle,
    }.get(family)
    if model is None:
        return None
    row = session.get(model, identifier)
    if (
        row is None
        or row.generation_id != baseline.generation_id
        or row.import_run_id is None
    ):
        return None

    active = session.get(models.ActiveSectionRegistry, baseline.generation_id)
    if active is None:
        return None
    registry = session.get(models.SectionRegistryVersion, active.registry_version_id)
    activation = session.get(models.SectionRegistryActivation, active.registry_activation_id)
    if (
        registry is None
        or registry.generation_id != baseline.generation_id
        or activation is None
        or activation.generation_id != baseline.generation_id
        or activation.registry_version_id != registry.registry_version_id
        or activation.activation_route != "import"
        or activation.import_run_id != registry.import_run_id
        or activation.registry_revision != active.registry_revision
        or row.import_run_id != registry.import_run_id
    ):
        return None
    import_run = session.get(models.ImportRun, registry.import_run_id)
    if (
        import_run is None
        or import_run.status != "complete"
        or import_run.legacy_generation_id != baseline.source_generation_identity
        or import_run.source_commit != baseline.source_commit
    ):
        return None
    return str(identifier)


def _capture_schema(envelope: tx.ShadowEnvelope) -> int:
    try:
        return int(dict(envelope.pinned_inputs or {}).get("capture_schema", 0))
    except (TypeError, ValueError):
        return 0


def _snapshot_rows(snapshot: Mapping[str, Any], table: str) -> list[Mapping[str, Any]]:
    tables = snapshot.get("tables")
    if not isinstance(tables, Mapping):
        return []
    rows = tables.get(table)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _lineage_scope_contains(
    envelope: tx.ShadowEnvelope, *, key: str, source_value: str
) -> bool:
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return False
    scope = snapshot.get("lineage_scope")
    if not isinstance(scope, Mapping):
        return False
    values = scope.get(key)
    return isinstance(values, list) and source_value in {str(item) for item in values}


def _source_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _source_request_result(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    encoded = (
        row.get("resolution_result_json")
        if row.get("status") == "completed" and row.get("resolution_result_json")
        else row.get("result_json")
    )
    if isinstance(encoded, Mapping):
        return encoded
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    try:
        parsed = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _source_operation_creator_request(
    envelope: tx.ShadowEnvelope, source_value: str
) -> Mapping[str, Any] | None:
    """Return the unique ordinary-start request that durably created a source operation."""
    if _capture_schema(envelope) < 3:
        return None
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    selected = snapshot.get("selected_tables")
    if not isinstance(selected, list) or not {"operations", "service_requests"}.issubset(
        {str(item) for item in selected}
    ):
        return None
    lineage_scope = snapshot.get("lineage_scope")
    if not isinstance(lineage_scope, Mapping):
        return None
    scoped_operations = lineage_scope.get("operation_ids")
    if not isinstance(scoped_operations, list) or source_value not in {
        str(item) for item in scoped_operations
    }:
        return None

    operations = [
        row
        for row in _snapshot_rows(snapshot, "operations")
        if str(row.get("operation_id") or "") == source_value
    ]
    if len(operations) != 1:
        return None
    operation = operations[0]
    operation_kind = str(operation.get("operation_kind") or "")
    if operation_kind not in {"planning", "initial", "change"}:
        return None
    task_gid = str(operation.get("task_gid") or "")
    operation_created_at = _source_timestamp(operation.get("created_at"))
    if not task_gid or operation_created_at is None:
        return None

    candidates: list[Mapping[str, Any]] = []
    for row in _snapshot_rows(snapshot, "service_requests"):
        if (
            str(row.get("operation_id") or "") != source_value
            or row.get("command") != "start"
            or row.get("status") != "completed"
            or str(row.get("task_gid") or "") != task_gid
        ):
            continue
        created_at = _source_timestamp(row.get("created_at"))
        completed_at = _source_timestamp(row.get("completed_at"))
        if (
            created_at is None
            or completed_at is None
            or not (created_at <= operation_created_at <= completed_at)
        ):
            continue
        result = _source_request_result(row)
        if result is None or result.get("ok") is not True or result.get("command") != "start":
            continue
        if (
            str(result.get("submission_id") or "") != source_value
            or str(result.get("task_gid") or "") != task_gid
        ):
            continue
        data = result.get("data")
        if not isinstance(data, Mapping):
            continue
        if (
            str(data.get("operation_id") or "") != source_value
            or str(data.get("operation_kind") or "") != operation_kind
        ):
            continue
        candidates.append(row)
    return candidates[0] if len(candidates) == 1 else None


def _baseline_for_envelope(session, envelope: tx.ShadowEnvelope) -> tx.ShadowBaseline | None:
    baseline = session.get(tx.ShadowBaseline, envelope.shadow_baseline_id)
    if (
        baseline is None
        or baseline.source_generation_identity != envelope.source_authority_generation
    ):
        return None
    return baseline


def _target_task_id_for_source_gid(session, source_task_gid: str) -> uuid.UUID | None:
    aliases = list(
        session.scalars(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.external_system == "asana",
                models.TaskExternalAlias.external_id == source_task_gid,
                models.TaskExternalAlias.state == "active",
            )
        )
    )
    return aliases[0].task_id if len(aliases) == 1 else None


def _source_operation_row(
    envelope: tx.ShadowEnvelope, source_value: str
) -> Mapping[str, Any] | None:
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    rows = [
        row
        for row in _snapshot_rows(snapshot, "operations")
        if str(row.get("operation_id") or "") == source_value
    ]
    return rows[0] if len(rows) == 1 else None


def _target_operation_from_creator_request(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    source_request = _source_operation_creator_request(envelope, source_value)
    if source_request is None:
        return None
    source_request_id = str(source_request.get("request_id") or "")
    if not source_request_id:
        return None

    baseline = _baseline_for_envelope(session, envelope)
    if baseline is None:
        return None
    target_request_id = _shadow_uuid(envelope, label="request", value=source_request_id)
    target_request = session.get(wf.ServiceRequest, target_request_id)
    if (
        target_request is None
        or target_request.generation_id != baseline.generation_id
        or target_request.command_name != "start"
    ):
        return None

    target_operations = list(
        session.scalars(
            select(wf.WorkflowOperation).where(
                wf.WorkflowOperation.generation_id == baseline.generation_id,
                wf.WorkflowOperation.creation_request_id == target_request_id,
            )
        )
    )
    if len(target_operations) != 1:
        return None
    target_operation = target_operations[0]
    if target_operation.import_run_id is not None:
        return None

    source_operation = _source_operation_row(envelope, source_value)
    if source_operation is None:
        return None
    task_gid = str(source_operation.get("task_gid") or "")
    target_task_id = _target_task_id_for_source_gid(session, task_gid)
    if (
        target_task_id is None
        or target_task_id != target_operation.task_id
        or target_operation.kind != str(source_operation.get("operation_kind") or "")
    ):
        return None
    return str(target_operation.operation_id)


def _target_operation_from_succession(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
    seen: set[str],
) -> str | None:
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    source_edges = [
        row
        for row in _snapshot_rows(snapshot, "operation_successions")
        if str(row.get("successor_operation_id") or "") == source_value
    ]
    if len(source_edges) != 1:
        return None
    source_edge = source_edges[0]
    source_predecessor = str(source_edge.get("source_operation_id") or "")
    if not source_predecessor:
        return None
    target_predecessor = _resolve_operation_identity_binding(
        session, envelope, source_value=source_predecessor, seen=seen
    )
    if target_predecessor is None:
        return None
    try:
        target_predecessor_id = uuid.UUID(target_predecessor)
    except ValueError:
        return None

    target_edges = list(
        session.scalars(
            select(wf.OperationSuccessionEdge).where(
                wf.OperationSuccessionEdge.source_operation_id == target_predecessor_id
            )
        )
    )
    if len(target_edges) != 1:
        return None
    target_edge = target_edges[0]
    target_operation = session.get(wf.WorkflowOperation, target_edge.successor_operation_id)
    source_operation = _source_operation_row(envelope, source_value)
    baseline = _baseline_for_envelope(session, envelope)
    if target_operation is None or source_operation is None or baseline is None:
        return None
    target_task_id = _target_task_id_for_source_gid(
        session, str(source_operation.get("task_gid") or "")
    )
    if (
        target_operation.generation_id != baseline.generation_id
        or target_task_id is None
        or target_operation.task_id != target_task_id
        or target_edge.task_id != target_task_id
        or target_operation.kind != str(source_operation.get("operation_kind") or "")
    ):
        return None
    return str(target_operation.operation_id)


def _resolve_operation_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
    seen: set[str] | None = None,
) -> str | None:
    imported = _imported_identity_binding(
        session, envelope, family="operation", source_value=source_value
    )
    if imported is not None:
        return imported
    if _capture_schema(envelope) < 3:
        return None
    active_seen = set() if seen is None else set(seen)
    if source_value in active_seen:
        return None
    active_seen.add(source_value)
    ordinary = _target_operation_from_creator_request(
        session, envelope, source_value=source_value
    )
    if ordinary is not None:
        return ordinary
    return _target_operation_from_succession(
        session, envelope, source_value=source_value, seen=active_seen
    )



def _live_verification_cycle_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    if _capture_schema(envelope) < 3 or not _lineage_scope_contains(
        envelope, key="cycle_ids", source_value=source_value
    ):
        return None
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    source_cycles = [
        row
        for row in _snapshot_rows(snapshot, "verification_cycles")
        if str(row.get("cycle_id") or "") == source_value
    ]
    if len(source_cycles) != 1:
        return None
    source_cycle = source_cycles[0]
    source_operation_id = str(source_cycle.get("operation_id") or "")
    target_operation = _resolve_operation_identity_binding(
        session, envelope, source_value=source_operation_id
    )
    if target_operation is None:
        return None
    try:
        target_operation_id = uuid.UUID(target_operation)
    except ValueError:
        return None

    successor_edges = [
        row
        for row in _snapshot_rows(snapshot, "operation_successions")
        if str(row.get("successor_operation_id") or "") == source_operation_id
        and str(row.get("successor_cycle_id") or "") == source_value
    ]
    if len(successor_edges) > 1:
        return None
    target_cycle = None
    if successor_edges:
        source_predecessor = str(successor_edges[0].get("source_operation_id") or "")
        target_predecessor = _resolve_operation_identity_binding(
            session, envelope, source_value=source_predecessor
        )
        if target_predecessor is None:
            return None
        try:
            target_predecessor_id = uuid.UUID(target_predecessor)
        except ValueError:
            return None
        target_edges = list(
            session.scalars(
                select(wf.OperationSuccessionEdge).where(
                    wf.OperationSuccessionEdge.source_operation_id == target_predecessor_id,
                    wf.OperationSuccessionEdge.successor_operation_id == target_operation_id,
                )
            )
        )
        if len(target_edges) != 1 or target_edges[0].prepared_cycle_id is None:
            return None
        target_cycle = session.get(wf.VerificationCycle, target_edges[0].prepared_cycle_id)
    else:
        try:
            cycle_sequence = int(source_cycle.get("cycle_number"))
        except (TypeError, ValueError):
            return None
        if cycle_sequence <= 0:
            return None
        target_cycles = list(
            session.scalars(
                select(wf.VerificationCycle).where(
                    wf.VerificationCycle.operation_id == target_operation_id,
                    wf.VerificationCycle.cycle_sequence == cycle_sequence,
                )
            )
        )
        if len(target_cycles) != 1:
            return None
        target_cycle = target_cycles[0]

    baseline = _baseline_for_envelope(session, envelope)
    if target_cycle is None or baseline is None:
        return None
    target_task_id = _target_task_id_for_source_gid(
        session, str(source_cycle.get("task_gid") or "")
    )
    if (
        target_cycle.generation_id != baseline.generation_id
        or target_cycle.import_run_id is not None
        or target_task_id is None
        or target_cycle.task_id != target_task_id
        or target_cycle.operation_id != target_operation_id
    ):
        return None
    return str(target_cycle.cycle_id)


def _live_or_imported_cycle_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    imported = _imported_identity_binding(
        session, envelope, family="verification_cycle", source_value=source_value
    )
    if imported is not None:
        return imported
    return _live_verification_cycle_identity_binding(
        session, envelope, source_value=source_value
    )


def _live_actor_lease_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    if _capture_schema(envelope) < 3 or not _lineage_scope_contains(
        envelope, key="lease_ids", source_value=source_value
    ):
        return None
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    source_leases = [
        row
        for row in _snapshot_rows(snapshot, "service_leases")
        if str(row.get("lease_id") or "") == source_value
    ]
    if len(source_leases) != 1:
        return None
    source_lease = source_leases[0]
    if str(source_lease.get("lease_kind") or "") != "actor":
        return None
    try:
        attempt_sequence = int(source_lease.get("actor_attempt_seq"))
    except (TypeError, ValueError):
        return None
    if attempt_sequence <= 0:
        return None

    target_operation = _resolve_operation_identity_binding(
        session,
        envelope,
        source_value=str(source_lease.get("operation_id") or ""),
    )
    if target_operation is None:
        return None
    try:
        target_operation_id = uuid.UUID(target_operation)
    except ValueError:
        return None
    target_task_id = _target_task_id_for_source_gid(
        session, str(source_lease.get("task_gid") or "")
    )
    baseline = _baseline_for_envelope(session, envelope)
    if target_task_id is None or baseline is None:
        return None
    targets = list(
        session.scalars(
            select(wf.ServiceLease).where(
                wf.ServiceLease.generation_id == baseline.generation_id,
                wf.ServiceLease.task_id == target_task_id,
                wf.ServiceLease.actor_attempt_sequence == attempt_sequence,
            )
        )
    )
    if len(targets) != 1:
        return None
    target = targets[0]
    owner_id = str(source_lease.get("owner_id") or "")
    source_run_id = str(source_lease.get("run_id") or "")
    if not owner_id or not source_run_id:
        return None
    expected_run_id = _shadow_uuid(
        envelope, label="run", value=f"{owner_id}:{source_run_id}"
    )
    if (
        target.import_run_id is not None
        or target.lease_kind != "actor"
        or target.operation_id != target_operation_id
        or target.owner_id != owner_id
        or target.run_id != expected_run_id
    ):
        return None
    source_cycle = str(source_lease.get("context_cycle_id") or "").strip()
    if source_cycle:
        target_cycle = _live_or_imported_cycle_identity_binding(
            session, envelope, source_value=source_cycle
        )
        if target_cycle is None or target.verification_cycle_id != uuid.UUID(target_cycle):
            return None
    elif target.verification_cycle_id is not None:
        return None
    return str(target.lease_id)


def _live_or_imported_lease_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    imported = _imported_identity_binding(
        session, envelope, family="lease", source_value=source_value
    )
    if imported is not None:
        return imported
    return _live_actor_lease_identity_binding(
        session, envelope, source_value=source_value
    )


def _live_planning_challenge_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    if _capture_schema(envelope) < 3 or not _lineage_scope_contains(
        envelope, key="challenge_ids", source_value=source_value
    ):
        return None
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    source_rows = [
        row
        for row in _snapshot_rows(snapshot, "planning_intent_challenges")
        if str(row.get("challenge_id") or "") == source_value
    ]
    if len(source_rows) != 1:
        return None
    source = source_rows[0]
    source_request_id = str(source.get("created_request_id") or "").strip()
    source_task_gid = str(source.get("task_gid") or "").strip()
    source_owner_id = str(source.get("owner_id") or "").strip()
    source_run_id = str(source.get("run_id") or "").strip()
    source_agent = str(source.get("agent") or "").strip()
    if not all((source_request_id, source_task_gid, source_owner_id, source_run_id, source_agent)):
        return None

    if not _lineage_scope_contains(
        envelope, key="explicit_request_ids", source_value=source_request_id
    ):
        return None
    source_requests = [
        row
        for row in _snapshot_rows(snapshot, "service_requests")
        if str(row.get("request_id") or "") == source_request_id
        and row.get("command") == "start"
        and row.get("status") == "completed"
        and str(row.get("task_gid") or "") == source_task_gid
        and str(row.get("owner_id") or "") == source_owner_id
        and str(row.get("run_id") or "") == source_run_id
    ]
    if len(source_requests) != 1:
        return None

    baseline = _baseline_for_envelope(session, envelope)
    target_task_id = _target_task_id_for_source_gid(session, source_task_gid)
    if baseline is None or target_task_id is None:
        return None
    target_request_id = _shadow_uuid(envelope, label="request", value=source_request_id)
    target_request = session.get(wf.ServiceRequest, target_request_id)
    if (
        target_request is None
        or target_request.generation_id != baseline.generation_id
        or target_request.command_name != "start"
    ):
        return None
    targets = list(
        session.scalars(
            select(wf.PlanningIntentChallenge).where(
                wf.PlanningIntentChallenge.generation_id == baseline.generation_id,
                wf.PlanningIntentChallenge.issuing_request_id == target_request_id,
            )
        )
    )
    if len(targets) != 1:
        return None
    target = targets[0]
    expected_run_id = _shadow_uuid(
        envelope, label="run", value=f"{source_owner_id}:{source_run_id}"
    )
    if (
        target.task_id != target_task_id
        or target.owner_id != source_owner_id
        or target.run_id != expected_run_id
        or target.agent != source_agent
        or target.target_kind != "planning"
    ):
        return None
    return str(target.challenge_id)


def _source_json_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _source_abandonment_creator_request_id(
    envelope: tx.ShadowEnvelope, source_value: str
) -> str | None:
    if _capture_schema(envelope) < 3 or not _lineage_scope_contains(
        envelope, key="abandonment_ids", source_value=source_value
    ):
        return None
    snapshot = envelope.source_pre_state
    if not isinstance(snapshot, Mapping):
        return None
    source_rows = [
        row
        for row in _snapshot_rows(snapshot, "abandonment_attempts")
        if str(row.get("abandonment_id") or "") == source_value
    ]
    if len(source_rows) != 1:
        return None
    source = source_rows[0]
    source_operation_id = str(source.get("source_operation_id") or "").strip()
    source_lease_id = str(source.get("source_lease_id") or "").strip()
    if not source_operation_id or not source_lease_id:
        return None

    execution_ids: set[str] = set()
    for event in _snapshot_rows(snapshot, "audit_events"):
        if (
            event.get("event_type") != "operation.abandonment_started"
            or str(event.get("operation_id") or "") != source_operation_id
        ):
            continue
        details = _source_json_mapping(event.get("details"))
        if details is None:
            continue
        if (
            str(details.get("abandonment_id") or "") != source_value
            or str(details.get("source_lease_id") or "") != source_lease_id
        ):
            continue
        execution_id = str(event.get("operation_execution_id") or "").strip()
        if execution_id:
            execution_ids.add(execution_id)
    if len(execution_ids) != 1:
        return None
    execution_id = next(iter(execution_ids))
    executions = [
        row
        for row in _snapshot_rows(snapshot, "operation_executions")
        if str(row.get("execution_id") or "") == execution_id
        and str(row.get("operation_id") or "") == source_operation_id
        and row.get("command") == "abandon-operation"
    ]
    if len(executions) != 1:
        return None
    request_id = str(executions[0].get("request_id") or "").strip()
    if not request_id:
        return None
    if not _lineage_scope_contains(
        envelope, key="explicit_request_ids", source_value=request_id
    ):
        return None
    requests = [
        row
        for row in _snapshot_rows(snapshot, "service_requests")
        if str(row.get("request_id") or "") == request_id
        and row.get("command") == "abandon-operation"
        and row.get("status") == "completed"
        and str(row.get("operation_id") or "") == source_operation_id
        and str(row.get("task_gid") or "") == str(source.get("task_gid") or "")
    ]
    return request_id if len(requests) == 1 else None


def _live_abandonment_identity_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    source_value: str,
) -> str | None:
    source_request_id = _source_abandonment_creator_request_id(envelope, source_value)
    if source_request_id is None:
        return None
    snapshot = envelope.source_pre_state
    assert isinstance(snapshot, Mapping)
    source_rows = [
        row
        for row in _snapshot_rows(snapshot, "abandonment_attempts")
        if str(row.get("abandonment_id") or "") == source_value
    ]
    if len(source_rows) != 1:
        return None
    source = source_rows[0]

    baseline = _baseline_for_envelope(session, envelope)
    if baseline is None:
        return None
    target_request_id = _shadow_uuid(envelope, label="request", value=source_request_id)
    target_request = session.get(wf.ServiceRequest, target_request_id)
    if (
        target_request is None
        or target_request.generation_id != baseline.generation_id
        or target_request.command_name != "abandon-operation"
    ):
        return None
    targets = list(
        session.scalars(
            select(wf.AbandonmentAttempt).where(
                wf.AbandonmentAttempt.generation_id == baseline.generation_id,
                wf.AbandonmentAttempt.request_id == target_request_id,
            )
        )
    )
    if len(targets) != 1:
        return None
    target = targets[0]

    source_operation = str(source.get("source_operation_id") or "").strip()
    source_lease = str(source.get("source_lease_id") or "").strip()
    target_operation = _resolve_operation_identity_binding(
        session, envelope, source_value=source_operation
    )
    target_lease = _live_or_imported_lease_identity_binding(
        session, envelope, source_value=source_lease
    )
    if target_operation is None or target_lease is None:
        return None
    source_task_gid = str(source.get("task_gid") or "").strip()
    target_task_id = _target_task_id_for_source_gid(session, source_task_gid)
    source_owner_id = str(source.get("abandoned_owner_id") or "").strip()
    source_run_id = str(source.get("abandoned_run_id") or "").strip()
    if target_task_id is None or not source_owner_id or not source_run_id:
        return None
    expected_run_id = _shadow_uuid(
        envelope, label="run", value=f"{source_owner_id}:{source_run_id}"
    )
    try:
        target_operation_id = uuid.UUID(target_operation)
        target_lease_id = uuid.UUID(target_lease)
    except ValueError:
        return None
    if (
        target.task_id != target_task_id
        or target.source_operation_id != target_operation_id
        or target.source_lease_id != target_lease_id
        or target.source_owner_id != source_owner_id
        or target.source_run_id != expected_run_id
    ):
        return None
    source_cycle = str(source.get("attempt_cycle_id") or "").strip()
    if source_cycle:
        target_cycle = _live_or_imported_cycle_identity_binding(
            session, envelope, source_value=source_cycle
        )
        if target_cycle is None or target.source_cycle_id != uuid.UUID(target_cycle):
            return None
    elif target.source_cycle_id is not None:
        return None
    target_execution = session.get(wf.CommandExecution, target.command_execution_id)
    if target_execution is None or target_execution.request_id != target_request_id:
        return None
    return str(target.abandonment_id)

def _resolve_identifier_binding(
    session,
    envelope: tx.ShadowEnvelope,
    *,
    family: str,
    source_value: str,
) -> str | None:
    if family == "operation":
        return _resolve_operation_identity_binding(
            session, envelope, source_value=source_value
        )
    if family == "verification_cycle":
        return _live_or_imported_cycle_identity_binding(
            session, envelope, source_value=source_value
        )
    if family == "lease":
        return _live_or_imported_lease_identity_binding(
            session, envelope, source_value=source_value
        )
    if family == "planning_challenge":
        return _live_planning_challenge_identity_binding(
            session, envelope, source_value=source_value
        )
    if family == "abandonment":
        return _live_abandonment_identity_binding(
            session, envelope, source_value=source_value
        )
    return _imported_identity_binding(
        session, envelope, family=family, source_value=source_value
    )


def _translate_workflow_identifiers(
    session,
    envelope: tx.ShadowEnvelope,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    translated = dict(arguments)
    needed = {
        key: _IDENTIFIER_ROLES[key][1]
        for key, value in arguments.items()
        if key in _IDENTIFIER_ROLES and value is not None and value != ""
    }
    if not needed:
        return translated
    for key, family in needed.items():
        source_value = str(arguments[key])
        target_value = _resolve_identifier_binding(
            session, envelope, family=family, source_value=source_value
        )
        if target_value is None:
            raise ShadowIdentityMappingError(
                f"no unique target {family} binding for captured field {key}"
            )
        translated[key] = target_value
    return translated


def _result_payload(result: CommandResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "command": result.command,
        "code": result.code,
        "http_status": result.http_status,
        "data": dict(result.data),
        "retryable": result.retryable,
    }


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in {None, ""}:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _source_task_reference(envelope: tx.ShadowEnvelope) -> str | None:
    for snapshot in (envelope.source_post_state, envelope.source_pre_state):
        if isinstance(snapshot, Mapping):
            values = snapshot.get("task_gids")
            if isinstance(values, list):
                for candidate in values:
                    value = str(candidate or "").strip()
                    if value:
                        return value
            value = str(snapshot.get("task_gid") or "").strip()
            if value:
                return value
    arguments = dict(envelope.canonical_input or {}).get("arguments")
    if isinstance(arguments, Mapping):
        value = str(arguments.get("task_gid") or arguments.get("task_id") or "").strip()
        if value:
            return value
    return None


def _target_authority_state(
    session,
    *,
    port: PostgresCommandPort,
    envelope: tx.ShadowEnvelope,
    arguments: Mapping[str, Any],
    request_id: uuid.UUID,
    result: CommandResult | None,
) -> dict[str, Any]:
    """Capture only target domains the bounded source snapshot selected."""
    source_projection = canonical_legacy_state(envelope.source_post_state)
    captured_domains = list(source_projection.get("captured_domains") or [])
    generation = port.reads.active_generation()
    task_ids: set[uuid.UUID] = set()
    operation_ids: set[uuid.UUID] = set()

    response_data = dict(result.data) if result is not None else {}
    for key in ("operation_id", "successor_operation_id"):
        parsed = _as_uuid(response_data.get(key) or arguments.get(key))
        if parsed is not None:
            operation_ids.add(parsed)
    parsed_argument_operation = _as_uuid(
        arguments.get("submission_id") or arguments.get("existing_submission_id")
    )
    if parsed_argument_operation is not None:
        operation_ids.add(parsed_argument_operation)
    for operation_id in tuple(operation_ids):
        operation = session.get(wf.WorkflowOperation, operation_id)
        if operation is not None and operation.generation_id == generation.generation_id:
            task_ids.add(operation.task_id)

    parsed_task = _as_uuid(response_data.get("task_id") or arguments.get("task_id"))
    if parsed_task is not None:
        task_ids.add(parsed_task)
    for reference in (
        arguments.get("task_gid"),
        _source_task_reference(envelope),
    ):
        if reference in {None, ""}:
            continue
        try:
            task_ids.add(port.reads.resolve_task(str(reference)).task_id)
        except ReadModelError:
            continue

    domains: dict[str, Any] = {}
    sorted_task_ids = sorted(task_ids, key=str)
    if "task_content" in captured_domains:
        rows: list[dict[str, Any]] = []
        for task_id in sorted_task_ids:
            head = session.get(models.TaskAuthorityHead, (generation.generation_id, task_id))
            if head is None:
                continue
            activation = session.get(models.ContentActivation, head.current_content_activation_id)
            version = (
                None
                if activation is None
                else session.get(models.ContentVersion, activation.content_version_id)
            )
            if version is not None:
                rows.append({
                    "identity": version.content_identity,
                    "title": version.title,
                    "body": version.body,
                })
        domains["task_content"] = sorted(rows, key=lambda row: repr(sorted(row.items())))

    if "operations" in captured_domains:
        query = select(wf.WorkflowOperation).where(
            wf.WorkflowOperation.generation_id == generation.generation_id
        )
        if sorted_task_ids:
            query = query.where(wf.WorkflowOperation.task_id.in_(sorted_task_ids))
        elif operation_ids:
            query = query.where(wf.WorkflowOperation.operation_id.in_(operation_ids))
        else:
            query = query.where(False)
        rows = []
        for row in session.scalars(query).all():
            phase = "terminal" if row.lifecycle != "open" or row.phase in {"completed", "cancelled"} else row.phase
            lifecycle = {
                "cancelled_by_marco": "cancelled",
                "abandoned": "cancelled",
                "failed": "uncertain",
            }.get(row.lifecycle, row.lifecycle)
            rows.append({
                "kind": row.kind,
                "lifecycle": lifecycle,
                "phase": phase,
                "terminal_outcome": row.terminal_outcome,
            })
        domains["operations"] = sorted(rows, key=lambda row: repr(sorted(row.items())))

    if "leases" in captured_domains:
        query = select(wf.ServiceLease).where(
            wf.ServiceLease.generation_id == generation.generation_id
        )
        query = query.where(wf.ServiceLease.task_id.in_(sorted_task_ids)) if sorted_task_ids else query.where(False)
        rows = [
            {
                "state": "active" if row.state == "active" else "terminal",
                "lease_kind": row.lease_kind,
                "actor_attempt_sequence": row.actor_attempt_sequence,
            }
            for row in session.scalars(query).all()
        ]
        domains["leases"] = sorted(rows, key=lambda row: repr(sorted(row.items())))

    if "verification_cycles" in captured_domains:
        query = select(wf.VerificationCycle).where(
            wf.VerificationCycle.generation_id == generation.generation_id
        )
        if operation_ids:
            query = query.where(wf.VerificationCycle.operation_id.in_(operation_ids))
        elif sorted_task_ids:
            query = query.where(wf.VerificationCycle.task_id.in_(sorted_task_ids))
        else:
            query = query.where(False)
        rows = [
            {"outcome": row.outcome, "open": row.terminal_at is None}
            for row in session.scalars(query).all()
        ]
        domains["verification_cycles"] = sorted(rows, key=lambda row: repr(sorted(row.items())))

    if "external_intents" in captured_domains:
        # Compare only intent classes represented by legacy attempt tables.
        # Create/completion/reproject rows remain raw evidence but have no
        # independently captured SQLite counterpart on this axis.
        event_map = {
            "update_task_document": "content_write",
            "move_task": "section_move",
        }
        query = select(tx.ProjectionOutboxEvent).where(
            tx.ProjectionOutboxEvent.generation_id == generation.generation_id,
            tx.ProjectionOutboxEvent.origin == "shadow",
        )
        if operation_ids:
            query = query.join(
                wf.CommandExecution,
                wf.CommandExecution.execution_id == tx.ProjectionOutboxEvent.command_execution_id,
            ).where(wf.CommandExecution.operation_id.in_(operation_ids))
        elif sorted_task_ids:
            query = query.where(tx.ProjectionOutboxEvent.task_id.in_(sorted_task_ids))
        else:
            query = query.where(False)
        rows = [
            {"kind": event_map[row.event_type]}
            for row in session.scalars(query).all()
            if row.event_type in event_map
        ]
        domains["external_intents"] = sorted(rows, key=lambda row: repr(sorted(row.items())))

    if "abandonments" in captured_domains:
        query = select(wf.AbandonmentAttempt).where(
            wf.AbandonmentAttempt.generation_id == generation.generation_id
        )
        if operation_ids:
            query = query.where(wf.AbandonmentAttempt.source_operation_id.in_(operation_ids))
        elif sorted_task_ids:
            query = query.where(wf.AbandonmentAttempt.task_id.in_(sorted_task_ids))
        else:
            query = query.where(False)
        domains["abandonments"] = sorted(
            [{"state": row.state} for row in session.scalars(query).all()],
            key=lambda row: repr(sorted(row.items())),
        )

    if "requests" in captured_domains:
        request = session.get(wf.ServiceRequest, request_id)
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        domains["requests"] = [] if request is None else [{
            "command": request.command_name,
            "status": "completed" if outcome is not None else "pending",
        }]

    return {"captured_domains": captured_domains, "domains": domains}


def _target_response_payload(
    session,
    *,
    port: PostgresCommandPort,
    arguments: Mapping[str, Any],
    result: CommandResult,
) -> dict[str, Any]:
    """Enrich the raw target response with shared lifecycle/action facts."""
    payload = _result_payload(result)
    data = dict(payload.get("data") or {})
    task = None
    operation = None
    operation_id = _as_uuid(
        data.get("operation_id")
        or data.get("successor_operation_id")
        or arguments.get("operation_id")
        or arguments.get("submission_id")
        or arguments.get("existing_submission_id")
    )
    if operation_id is not None:
        operation = session.get(wf.WorkflowOperation, operation_id)
        if operation is not None:
            task = session.get(models.DishTask, operation.task_id)
    task_reference = data.get("task_id") or arguments.get("task_id") or arguments.get("task_gid")
    if task is None and task_reference not in {None, ""}:
        try:
            task = port.reads.resolve_task(str(task_reference))
        except ReadModelError:
            task = None
    if task is not None:
        data.setdefault("task_id", str(task.task_id))
        try:
            view = port.reads.task_view(task.task_id)
        except ReadModelError:
            view = None
        if view is not None:
            data["allowed_actions"] = list(view.legal_actions)
            if operation is None and view.operation_id is not None:
                operation = session.get(wf.WorkflowOperation, view.operation_id)
    if operation is not None:
        data.setdefault("operation_id", str(operation.operation_id))
        data["state"] = {
            "cancelled_by_marco": "cancelled",
            "abandoned": "cancelled",
            "failed": "uncertain",
        }.get(operation.lifecycle, operation.lifecycle)
    payload["data"] = data
    return payload


def semantic_normalizer(value: Mapping[str, Any]) -> Any:
    """Normalize only transport/replay metadata, never workflow semantics."""
    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): clean(child)
                for key, child in item.items()
                if key not in {"request_replayed", "captured_at", "service_cleanup_warning"}
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return clean(value)


class ShadowEvaluator(Protocol):
    def evaluate(self, session, envelope: tx.ShadowEnvelope) -> Mapping[str, Any]: ...


class CommandPortShadowEvaluator:
    def __init__(self, *, cursor_secret: bytes) -> None:
        self.cursor_secret = cursor_secret

    def evaluate(self, session, envelope: tx.ShadowEnvelope) -> Mapping[str, Any]:
        canonical = dict(envelope.canonical_input)
        arguments = canonical.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("shadow envelope arguments are missing")
        baseline = session.get(tx.ShadowBaseline, envelope.shadow_baseline_id)
        if baseline is None or baseline.status != "open":
            raise ValueError("shadow baseline is not open")
        principal = dict(envelope.principal or {})
        owner_id = str(principal.get("owner_id") or "legacy-shadow")
        source_run_identity = principal.get("run_id") or f"request:{envelope.source_request_identity}"
        target_run_id = _shadow_uuid(
            envelope, label="run", value=f"{owner_id}:{source_run_identity}"
        )
        target_request_id = _shadow_uuid(
            envelope, label="request", value=envelope.source_request_identity
        )
        port = PostgresCommandPort(
            session, cursor_secret=self.cursor_secret, projection_origin="shadow"
        )
        generation = port.reads.active_generation()
        if generation.generation_id != baseline.generation_id:
            raise ValueError("shadow baseline target generation is stale")
        translated_arguments = _translate_workflow_identifiers(session, envelope, arguments)
        _ensure_shadow_run(
            session,
            envelope=envelope,
            arguments=arguments,
            owner_id=owner_id,
            source_run_identity=source_run_identity,
            target_run_id=target_run_id,
            generation_id=baseline.generation_id,
        )
        pre_state = _target_authority_state(
            session,
            port=port,
            envelope=envelope,
            arguments=translated_arguments,
            request_id=target_request_id,
            result=None,
        )
        result = port.execute(
            CommandCall(
                command_name=envelope.command_name,
                arguments=translated_arguments,
                owner_id=owner_id,
                principal_class=str(principal.get("principal_class") or "agent"),
                run_id=target_run_id,
                request_id=target_request_id,
                now=envelope.captured_at,
            )
        )
        session.flush()
        post_state = _target_authority_state(
            session,
            port=port,
            envelope=envelope,
            arguments=translated_arguments,
            request_id=target_request_id,
            result=result,
        )
        return ShadowEvaluation(
            response=_target_response_payload(
                session, port=port, arguments=translated_arguments, result=result
            ),
            pre_state=pre_state,
            post_state=post_state,
            effects=canonical_transition(pre_state, post_state),
        ).as_payload()


class ShadowWorker:
    def __init__(
        self,
        *,
        spool: ShadowSpool,
        session_maker,
        baseline_id: uuid.UUID,
        evaluator: ShadowEvaluator,
        worker_id: str,
        comparator_release: str,
        kill_switch_path: Path,
        claim_ttl: timedelta = timedelta(minutes=2),
        reservation_ttl: timedelta = timedelta(seconds=RECOVERY_QUARANTINE_SECONDS),
        delivered_retention: timedelta = timedelta(days=7),
        idle_seconds: float = 1.0,
        clock=_utcnow,
    ) -> None:
        self.spool = spool
        self.session_maker = session_maker
        self.baseline_id = baseline_id
        self.evaluator = evaluator
        self.worker_id = worker_id
        self.comparator_release = comparator_release
        self.kill_switch_path = Path(kill_switch_path)
        require_distinct_paths({
            "dark-launch spool": self.spool.path,
            "dark-launch kill switch": self.kill_switch_path,
        })
        if reservation_ttl.total_seconds() < RECOVERY_QUARANTINE_SECONDS:
            raise ValueError(
                f"reservation_ttl must be at least {RECOVERY_QUARANTINE_SECONDS} seconds"
            )
        if delivered_retention.total_seconds() < 0:
            raise ValueError("delivered_retention must not be negative")
        self.claim_ttl = claim_ttl
        self.reservation_ttl = reservation_ttl
        self.delivered_retention = delivered_retention
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._stop = False

    def request_shutdown(self) -> None:
        self._stop = True

    def _kill_switch_engaged(self) -> bool:
        return self.kill_switch_path.exists()

    def _baseline_is_current(self, *, at: datetime) -> bool:
        with session_scope(self.session_maker) as session:
            active = session.scalar(
                select(models.AuthorityGeneration).where(
                    models.AuthorityGeneration.status == "active"
                )
            )
            if active is None:
                return False
            ShadowService(session).disqualify_stale_baselines(
                active_generation_id=active.generation_id,
                reason="active target authority generation changed during dark launch",
                at=at,
            )
            baseline = session.get(tx.ShadowBaseline, self.baseline_id)
            return bool(
                baseline is not None
                and baseline.status == "open"
                and baseline.generation_id == active.generation_id
            )

    def run_forever(self) -> None:
        while not self._stop:
            if self._kill_switch_engaged():
                LOGGER.warning(
                    "dark-launch kill switch engaged; shadow worker exiting",
                    extra={"kill_switch": str(self.kill_switch_path)},
                )
                return
            if not self.run_once():
                time.sleep(self.idle_seconds)

    def run_once(self) -> bool:
        if self._kill_switch_engaged():
            return False
        now = self.clock()
        if not self._baseline_is_current(at=now):
            LOGGER.error("shadow baseline is stale or unavailable; worker is not draining")
            return False
        try:
            self.spool.recover_stale_reservations(now=now, older_than=self.reservation_ttl)
        except BaseException:
            LOGGER.exception("shadow spool stale-reservation recovery failed")
            return False
        try:
            self.spool.compact_delivered(
                now=now, older_than=self.delivered_retention, limit=1000
            )
        except BaseException:
            LOGGER.exception("shadow spool delivered-payload compaction failed")
        pending = self.spool.pending(limit=1)
        if pending:
            item = pending[0]
            try:
                self._deliver(item)
            except BaseException as exc:
                LOGGER.exception("shadow spool delivery failed")
                try:
                    self.spool.mark_delivery_failed(item.registration_id, error=str(exc))
                except BaseException:
                    LOGGER.exception("shadow spool delivery-failure recording failed")
                return False
            delivered_at = self.clock()
            self.spool.mark_delivered(item.registration_id, delivered_at=delivered_at)
            try:
                self.spool.compact_delivered(
                    now=delivered_at, older_than=self.delivered_retention, limit=1000
                )
            except BaseException:
                LOGGER.exception("shadow spool delivered-payload compaction failed")
            if item.state == "gap":
                return True
        return self._evaluate_one() or bool(pending)

    def _deliver(self, item: ShadowSpoolItem) -> None:
        with session_scope(self.session_maker) as session:
            service = ShadowService(session)
            if item.state == "gap":
                service.record_gap(
                    baseline_id=self.baseline_id,
                    gap_identity=f"capture:{item.source_request_identity}",
                    gap_kind="missing_envelope",
                    details=dict(item.gap or {}),
                    created_at=item.completed_at or item.created_at,
                )
                return
            if item.source_outcome is None or item.source_post_state is None:
                raise ValueError("complete spool item lacks outcome or post-state")
            service.capture_envelope(
                shadow_baseline_id=self.baseline_id,
                command_name=item.command_name,
                source_request_identity=item.source_request_identity,
                canonical_input=item.canonical_input,
                source_outcome=item.source_outcome,
                source_post_state=item.source_post_state,
                captured_at=item.completed_at or item.created_at,
                rollout_sequence=item.rollout_sequence,
                source_authority_generation=item.source_authority_generation,
                source_execution_identity=item.source_request_identity,
                principal=item.principal,
                source_pre_state=item.source_pre_state,
                pinned_inputs=item.pinned_inputs,
                source_effects=item.source_effects,
                capture_qualification=item.treatment,
            )

    def _evaluate_one(self) -> bool:
        token = uuid.uuid4()
        with session_scope(self.session_maker) as session:
            delivery = ShadowService(session).claim_delivery(
                worker_id=self.worker_id,
                claim_token=token,
                now=self.clock(),
                ttl=self.claim_ttl,
                shadow_baseline_id=self.baseline_id,
            )
            if delivery is None:
                return False
            delivery_id = delivery.delivery_id
            envelope_id = delivery.envelope_id
            claim_revision = delivery.delivery_revision
        try:
            with session_scope(self.session_maker) as session:
                envelope = session.get(tx.ShadowEnvelope, envelope_id)
                rollout_mode = dict(envelope.pinned_inputs or {}).get("rollout_mode")
                treatment = treatment_for(envelope.command_name)
                if envelope.capture_qualification != treatment.treatment:
                    raise ValueError(
                        "captured dark-launch treatment contradicts authoritative command treatment: "
                        f"{envelope.command_name} captured={envelope.capture_qualification} "
                        f"authoritative={treatment.treatment}"
                    )
                if rollout_mode != "execute" or not treatment.comparison_eligible:
                    reason = (
                        f"dark-launch rollout mode is {rollout_mode or 'capture'}"
                        if rollout_mode != "execute"
                        else f"dark-launch treatment is {treatment.treatment}"
                    )
                    ShadowService(session).skip_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        claim_revision=claim_revision,
                        worker_id=self.worker_id,
                        reason=reason,
                        comparator_release=self.comparator_release,
                        completed_at=self.clock(),
                    )
                else:
                    target = self.evaluator.evaluate(session, envelope)
                    ShadowService(session).compare_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        claim_revision=claim_revision,
                        worker_id=self.worker_id,
                        target_result=target,
                        comparator_release=self.comparator_release,
                        compared_at=self.clock(),
                        semantic_normalizer=semantic_normalizer,
                    )
        except BaseException as exc:
            try:
                with session_scope(self.session_maker) as session:
                    ShadowService(session).fail_delivery(
                        delivery_id=delivery_id,
                        claim_token=token,
                        claim_revision=claim_revision,
                        worker_id=self.worker_id,
                        error=str(exc),
                        failed_at=self.clock(),
                    )
            except TransitionAuthorityError:
                LOGGER.info(
                    "shadow evaluation lost delivery authority; current state is preserved",
                    extra={"delivery_id": str(delivery_id)},
                )
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drain legacy dark-launch captures")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--spool-path", required=True, type=Path)
    parser.add_argument("--baseline-id", required=True, type=uuid.UUID)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--cursor-secret-file", required=True, type=Path)
    parser.add_argument("--comparator-release", required=True)
    parser.add_argument("--kill-switch", required=True, type=Path)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--busy-timeout-ms", type=int, default=50)
    parser.add_argument("--max-spool-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--max-spool-records", type=int, default=100_000)
    parser.add_argument("--min-free-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument(
        "--reservation-ttl-seconds", type=int, default=RECOVERY_QUARANTINE_SECONDS
    )
    parser.add_argument("--delivered-retention-seconds", type=int, default=7 * 24 * 60 * 60)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    for name in (
        "busy_timeout_ms",
        "max_spool_bytes",
        "max_spool_records",
        "min_free_bytes",
        "reservation_ttl_seconds",
        "delivered_retention_seconds",
    ):
        if getattr(args, name) <= 0:
            raise SystemExit(f"{name.replace('_', '-')} must be positive")
    if args.reservation_ttl_seconds < RECOVERY_QUARANTINE_SECONDS:
        raise SystemExit(
            f"reservation-ttl-seconds must be at least {RECOVERY_QUARANTINE_SECONDS}"
        )
    if args.delivered_retention_seconds < args.reservation_ttl_seconds:
        raise SystemExit(
            "delivered-retention-seconds must be at least reservation-ttl-seconds"
        )
    selected = make_url(args.database_url)
    if selected.get_backend_name() != "postgresql":
        raise SystemExit("shadow worker requires PostgreSQL")
    if selected.database != args.expected_database_name:
        raise SystemExit("PostgreSQL URL database does not match expected database name")
    secret = args.cursor_secret_file.read_bytes().strip()
    if len(secret) < 32:
        raise SystemExit("cursor secret must contain at least 32 bytes")
    engine = create_database_engine(DatabaseSettings(url=args.database_url))
    factory = session_factory(engine)
    try:
        with engine.connect() as connection:
            database_name = str(connection.scalar(text("SELECT current_database()")))
        if database_name != args.expected_database_name:
            raise SystemExit(
                "connected PostgreSQL database does not match expected database name"
            )
        with session_scope(factory) as session:
            baseline = session.get(tx.ShadowBaseline, args.baseline_id)
            if baseline is None:
                raise SystemExit(f"shadow baseline {args.baseline_id} does not exist")
            epoch = session.scalar(
                select(tx.ProjectionEpoch).where(
                    tx.ProjectionEpoch.generation_id == baseline.generation_id,
                    tx.ProjectionEpoch.status == "active",
                )
            )
            if epoch is None:
                raise SystemExit(
                    "shadow worker requires an active, effects-disabled projection epoch; "
                    "run `dish-pg-dark-launch activate-epoch --database-url ... "
                    f"--generation-id {baseline.generation_id} --reason ...` first"
                )
            if epoch.external_effects_enabled:
                raise SystemExit(
                    "shadow worker refuses an active projection epoch with external effects enabled"
                )
    except BaseException:
        engine.dispose()
        raise
    worker = ShadowWorker(
        spool=ShadowSpool.open_existing(
            args.spool_path,
            busy_timeout_ms=args.busy_timeout_ms,
            max_bytes=args.max_spool_bytes,
            max_records=args.max_spool_records,
            min_free_bytes=args.min_free_bytes,
        ),
        session_maker=factory,
        baseline_id=args.baseline_id,
        evaluator=CommandPortShadowEvaluator(cursor_secret=secret),
        worker_id=args.worker_id,
        comparator_release=args.comparator_release,
        kill_switch_path=args.kill_switch,
        reservation_ttl=timedelta(seconds=args.reservation_ttl_seconds),
        delivered_retention=timedelta(seconds=args.delivered_retention_seconds),
        idle_seconds=args.idle_seconds,
    )
    def stop(signum: int, frame: FrameType | None) -> None:
        del frame
        LOGGER.info("shutdown requested", extra={"signal": signum})
        worker.request_shutdown()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        worker.run_forever()
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
