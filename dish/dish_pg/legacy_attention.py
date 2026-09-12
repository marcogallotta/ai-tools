"""Current read/resolution view over imported legacy attention audit events."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import stage3_models as wf

ATTENTION_EVENT = "legacy_attention_imported"
RESOLUTION_EVENT = "legacy_attention_resolved"


def unresolved_legacy_attention(
    session: Session, generation_id: uuid.UUID
) -> list[dict[str, Any]]:
    imports = session.scalars(
        select(wf.GovernedAuditEvent).where(
            wf.GovernedAuditEvent.generation_id == generation_id,
            wf.GovernedAuditEvent.event_type == ATTENTION_EVENT,
        )
    )
    resolved = {
        str(event.payload.get("attention_id") or "")
        for event in session.scalars(
            select(wf.GovernedAuditEvent).where(
                wf.GovernedAuditEvent.generation_id == generation_id,
                wf.GovernedAuditEvent.event_type == RESOLUTION_EVENT,
            )
        )
    }
    rows = []
    for event in imports:
        if str(event.audit_event_id) in resolved:
            continue
        item = dict(event.payload["record"])
        item.update(
            {
                "attention_id": str(event.audit_event_id),
                "dish_id": str(event.task_id),
                "task_id": str(event.task_id),
                "source_operation_id": item.pop("operation_id", None),
            }
        )
        signals = [dict(signal) for signal in item.get("signals", [])]
        if signals:
            signals[0]["shell_command"] = (
                f"dish-admin resolve-legacy-attention {event.audit_event_id} --resolution TEXT"
            )
        item["signals"] = signals
        rows.append(item)
    return rows


__all__ = ["ATTENTION_EVENT", "RESOLUTION_EVENT", "unresolved_legacy_attention"]
