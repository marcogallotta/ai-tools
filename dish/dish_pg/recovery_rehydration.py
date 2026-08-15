"""Durable identity and result shape for destructive-restore authority rehydration."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

RECOVERY_REHYDRATION_REVISION = "recovery-rehydration-v1"


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
