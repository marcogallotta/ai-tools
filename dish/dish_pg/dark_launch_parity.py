"""Contained dark-launch parity shims kept outside the post-cutover protocol."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import event, select

from . import models
from . import stage5_models as tx

_PLANNING_CONFIRMATION_CODES = {
    "CONFIRMATION_REQUIRED",
    "PLANNING_CONFIRMATION_NOT_YET_ISSUED",
}
_OPERATION_ARGUMENT_KEYS = {
    "submission_id",
    "operation_id",
    "existing_submission_id",
    "target_operation_id",
    "prepared_operation_id",
    "successor_operation_id",
}
_OPERATION_BINDING_ERROR_PREFIX = "no unique target operation binding for captured field "
_CASCADE_CLASSIFICATION = "unbound_create_operation_binding_cascade"


def legacy_compatible_shadow_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Match legacy response semantics only at the dark-launch comparison boundary."""
    response = dict(value)
    data = dict(response.get("data") or {}) if isinstance(response.get("data"), Mapping) else {}
    command = str(response.get("command") or "")
    code = str(response.get("code") or "")
    planning_confirmation = command == "start" and (
        code == "PLANNING_CONFIRMATION_NOT_YET_ISSUED"
        or (
            code == "CONFIRMATION_REQUIRED"
            and (
                "intent_challenge_id" in data
                or "required_intent_basis" in data
            )
        )
    )
    if planning_confirmation:
        response["code"] = "CONFIRMATION_REQUIRED"
        response["retryable"] = True
        data["allowed_actions"] = ["start"]
        data["required_start_kind"] = "planning"
    elif command == "create" and response.get("ok") is True:
        data["allowed_actions"] = ["start"]
        data["required_start_kind"] = "planning"
    response["data"] = data
    return response


def _snapshot_rows(snapshot: Mapping[str, Any] | None, table: str) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    tables = snapshot.get("tables")
    if not isinstance(tables, Mapping):
        return []
    rows = tables.get(table)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _source_task_gid_for_operation_binding(envelope: Mapping[str, Any]) -> str | None:
    canonical_input = envelope.get("canonical_input")
    source_pre_state = envelope.get("source_pre_state")
    if not isinstance(canonical_input, Mapping) or not isinstance(source_pre_state, Mapping):
        return None
    arguments = canonical_input.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    operation_ids = {
        str(arguments[key])
        for key in _OPERATION_ARGUMENT_KEYS
        if arguments.get(key) not in {None, ""}
    }
    if not operation_ids:
        return None
    task_gids = {
        str(row.get("task_gid") or "").strip()
        for row in _snapshot_rows(source_pre_state, "operations")
        if str(row.get("operation_id") or "") in operation_ids
        and str(row.get("task_gid") or "").strip()
    }
    return next(iter(task_gids)) if len(task_gids) == 1 else None


def _source_create_task_gid(source_outcome: Any) -> str | None:
    if not isinstance(source_outcome, Mapping):
        return None
    data = source_outcome.get("data")
    if not isinstance(data, Mapping):
        return None
    value = str(data.get("task_gid") or "").strip()
    return value or None


def _unbound_create_cascade_details(connection, gap: tx.ShadowGap) -> dict[str, Any] | None:
    if gap.gap_kind != "delivery_failure" or gap.envelope_id is None:
        return None
    details = dict(gap.details or {})
    error = str(details.get("error") or "")
    if _OPERATION_BINDING_ERROR_PREFIX not in error:
        return None

    envelope = connection.execute(
        select(
            tx.ShadowEnvelope.shadow_baseline_id,
            tx.ShadowEnvelope.canonical_input,
            tx.ShadowEnvelope.source_pre_state,
            tx.ShadowEnvelope.captured_at,
        ).where(tx.ShadowEnvelope.envelope_id == gap.envelope_id)
    ).mappings().one_or_none()
    if envelope is None:
        return None
    source_task_gid = _source_task_gid_for_operation_binding(envelope)
    if source_task_gid is None:
        return None

    active_alias = connection.execute(
        select(models.TaskExternalAlias.alias_id).where(
            models.TaskExternalAlias.external_system == "asana",
            models.TaskExternalAlias.external_id == source_task_gid,
            models.TaskExternalAlias.state == "active",
        )
    ).first()
    if active_alias is not None:
        return None

    candidates = []
    create_rows = connection.execute(
        select(
            tx.ShadowEnvelope.envelope_id,
            tx.ShadowEnvelope.source_request_identity,
            tx.ShadowEnvelope.source_outcome,
            tx.ShadowEnvelope.capture_qualification,
        ).where(
            tx.ShadowEnvelope.shadow_baseline_id == envelope["shadow_baseline_id"],
            tx.ShadowEnvelope.command_name == "create",
            tx.ShadowEnvelope.captured_at <= envelope["captured_at"],
        )
    ).mappings().all()
    for row in create_rows:
        if _source_create_task_gid(row["source_outcome"]) == source_task_gid:
            candidates.append(row)
    if len(candidates) != 1:
        return None
    create_envelope = candidates[0]
    create_delivery_state = connection.execute(
        select(tx.ShadowDelivery.state).where(
            tx.ShadowDelivery.envelope_id == create_envelope["envelope_id"]
        )
    ).scalar_one_or_none()
    if (
        create_envelope["capture_qualification"] != "capture_only"
        and create_delivery_state not in {"delivered", "failed"}
    ):
        return None
    return {
        "error_classification": _CASCADE_CLASSIFICATION,
        "source_task_gid": source_task_gid,
        "create_source_request_identity": create_envelope["source_request_identity"],
        "create_capture_qualification": create_envelope["capture_qualification"],
        "create_delivery_state": create_delivery_state,
    }


@event.listens_for(tx.ShadowGap, "before_insert")
def _classify_unbound_create_cascade(_mapper, connection, gap: tx.ShadowGap) -> None:
    """Reclassify only proven unbound-create cascades; generic failures stay generic."""
    classification = _unbound_create_cascade_details(connection, gap)
    if classification is None:
        return
    gap.gap_kind = "uncomparable"
    gap.details = {**dict(gap.details or {}), **classification}
