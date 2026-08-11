"""Immutable read and evaluation values for Stage A release authority."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ReleaseCandidateStatus:
    candidate_id: uuid.UUID
    generation_id: uuid.UUID
    projection_epoch_id: uuid.UUID
    identity_contract_version: str | None
    source_manifest_sha256: str | None
    rehearsal_environment_identity: str | None
    registry_version_id: uuid.UUID | None
    honest_binding_id: uuid.UUID | None
    source_release: str
    source_commit: str
    schema_head: str
    dish_release: str
    honest_release: str
    protocol_release: str
    openapi_release: str
    routing_release: str
    status: str


@dataclass(frozen=True)
class WriterFenceStatus:
    fence_id: uuid.UUID
    candidate_id: uuid.UUID
    target_identity: str
    manifest_sha256: str
    state: str
    proof_sha256: str | None


@dataclass(frozen=True)
class AcceptanceCheck:
    code: str
    passed: bool
    details: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "details": dict(self.details)}


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: uuid.UUID
    checks: tuple[AcceptanceCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": str(self.candidate_id),
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }
