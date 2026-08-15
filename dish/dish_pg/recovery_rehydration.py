"""Durable identity and result shapes for destructive-restore authority recovery."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

RECOVERY_REHYDRATION_REVISION = "recovery-rehydration-v1"
RECOVERY_QUALIFICATION_REVISION = "recovery-qualification-v1"
RECOVERY_READINESS_REVISION = "recovery-readiness-v1"


@dataclass(frozen=True)
class RecoveryRehydrationResult:
    predecessor_generation_id: uuid.UUID
    generation_id: uuid.UUID
    import_run_id: uuid.UUID
    repair_event_id: uuid.UUID
    predecessor_snapshot_sha256: str
    transient_state_sha256: str
    task_count: int
    rehydrated_at: datetime
    replayed: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "predecessor_generation_id": str(self.predecessor_generation_id),
            "generation_id": str(self.generation_id),
            "import_run_id": str(self.import_run_id),
            "repair_event_id": str(self.repair_event_id),
            "predecessor_snapshot_sha256": self.predecessor_snapshot_sha256,
            "transient_state_sha256": self.transient_state_sha256,
            "task_count": self.task_count,
            "rehydrated_at": self.rehydrated_at.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class RecoveryQualificationSpec:
    request_id: uuid.UUID
    run_id: uuid.UUID
    owner_id: str
    principal_class: str
    command_name: str
    canonical_payload: Mapping[str, Any]


@dataclass(frozen=True)
class RecoveryQualificationResult:
    generation_id: uuid.UUID
    qualification_event_id: uuid.UUID
    request_id: uuid.UUID
    canonical_payload_sha256: str
    authorized_at: datetime
    replayed: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "generation_id": str(self.generation_id),
            "qualification_event_id": str(self.qualification_event_id),
            "request_id": str(self.request_id),
            "canonical_payload_sha256": self.canonical_payload_sha256,
            "authorized_at": self.authorized_at.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class RecoveryReadinessResult:
    generation_id: uuid.UUID
    readiness_event_id: uuid.UUID
    qualification_event_id: uuid.UUID
    request_id: uuid.UUID
    verified_at: datetime
    replayed: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "generation_id": str(self.generation_id),
            "readiness_event_id": str(self.readiness_event_id),
            "qualification_event_id": str(self.qualification_event_id),
            "request_id": str(self.request_id),
            "verified_at": self.verified_at.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "replayed": self.replayed,
        }
