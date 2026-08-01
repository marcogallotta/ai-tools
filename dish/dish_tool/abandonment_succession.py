"""Typed specification for one atomic abandonment successor transition."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .models import utc_now


@dataclass(frozen=True)
class AbandonmentSuccessionSpec:
    """All durable evidence required to terminalize a source and create its successor."""

    abandonment_id: str
    succession_id: str
    successor_operation_id: str
    source_content_version_id: str
    successor_content_version_id: str
    successor_operation_kind: str
    successor_phase: str
    successor_expected_section_gid: str
    successor_schema_version: str
    successor_claim_mode: str
    transition_reason: str
    candidate_transfer_kind: str
    source_cycle_id: str | None = None
    close_source_cycle_as_abandoned: bool = False
    successor_cycle_id: str | None = None
    successor_cycle_number: int | None = None
    successor_protocol_release: str | None = None
    successor_protocol_text: str | None = None
    successor_editor_agent: str | None = None
    successor_researcher_agent: str | None = None
    successor_verifier_agent: str | None = None
    successor_run_id: str | None = None
    successor_independence_attestation: str | None = None
    successor_actor_facts: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    successor_completed_steps: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict
    )
    result: Mapping[str, Any] | None = None
    created_at: str | None = None

    def normalized(self) -> "AbandonmentSuccessionSpec":
        """Freeze caller-owned containers and fill the durable timestamp once."""

        return replace(
            self,
            successor_actor_facts=tuple(dict(fact) for fact in self.successor_actor_facts),
            successor_completed_steps={
                str(name): dict(step)
                for name, step in self.successor_completed_steps.items()
            },
            result=None if self.result is None else dict(self.result),
            created_at=self.created_at or utc_now(),
        )
