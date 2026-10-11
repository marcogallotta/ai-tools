"""Seed legacy Verification Evidence holds for resume/admin coverage.

Agents can no longer open an Evidence hold: ``reject(route=evidence)`` is refused.
Holds opened before that change still exist and must keep resolving through
admin ``supply-evidence``. This fixture reinstalls the removed verification-time
Evidence branch of ``PostgresCommandPort._reject`` for one test, so the seeded
state matches exactly what the legacy route persisted.
"""
from __future__ import annotations

from typing import Any

from dish_pg.command_port import PostgresCommandPort
from dish_pg.document_authority import held_document, parse_canonical_document
from dish_pg import models


def _legacy_evidence_reject(
    self, call, generation, binding, execution, task, operation
) -> dict[str, Any]:
    cycle = self._latest_cycle(operation.operation_id)
    self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
    _inspection, actor = self._exact_verifier_inspection(call, cycle)
    reason = str(call.arguments.get("reason", "")).strip()
    reviewed = self.session.get(models.ContentVersion, cycle.reviewed_content_version_id)
    reviewed_parts = parse_canonical_document(
        title=reviewed.title,
        body=reviewed.body,
        expected_status="pending-verification",
        expected_schema_version=binding.schema_release,
    )
    cycle.lifecycle = "rejected"
    cycle.outcome = "rejected"
    cycle.terminal_at = call.now
    held_parts = held_document(
        reviewed_parts.document, target="pending-evidence", detail=reason
    )
    held_version_id = self._activate_document(
        generation_id=generation.generation_id,
        task_id=task.task_id,
        binding_id=binding.binding_id,
        execution_id=execution.execution_id,
        title=held_parts.title,
        body=held_parts.body,
        predecessor_content_version_id=reviewed.content_version_id,
        at=call.now,
    )
    self._finalize_scalar_mutations()
    projection_id = self._project(
        generation.generation_id,
        execution.execution_id,
        task.task_id,
        "update_task_document",
        {"content_version_id": str(held_version_id)},
        call.now,
    )
    hold = self.workflow.open_evidence_hold(
        hold_id=self.uuid_factory(),
        execution_id=execution.execution_id,
        operation_id=operation.operation_id,
        baseline_content_version_id=held_version_id,
        reason=reason,
        opened_at=call.now,
        cycle_id=cycle.cycle_id,
    )
    operation.phase = "held_evidence"
    operation.persisted_actions = ["supply-evidence"]
    self._release_verifier_lease(
        call=call,
        execution=execution,
        cycle=cycle,
        actor=actor,
        reason="verification rejected: evidence",
    )
    operation.operation_revision += 1
    return {
        "route": "evidence",
        "hold_id": str(hold.hold_id),
        "held_content_version_id": str(held_version_id),
        "projection_event_id": projection_id,
        "cycle_id": str(cycle.cycle_id),
    }


def install_legacy_evidence_reject(monkeypatch) -> None:
    """Let ``reject(route=evidence)`` seed a legacy hold for the current test only."""

    current = PostgresCommandPort._reject

    def _reject(self, call, generation, binding, execution, task, operation):
        route = str(call.arguments.get("route", "")).replace("_", "-").strip()
        if route == "evidence":
            return _legacy_evidence_reject(
                self, call, generation, binding, execution, task, operation
            )
        return current(self, call, generation, binding, execution, task, operation)

    monkeypatch.setattr(PostgresCommandPort, "_reject", _reject)
