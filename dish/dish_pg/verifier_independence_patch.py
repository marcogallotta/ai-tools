"""Narrow runtime correction for PostgreSQL verifier independence parity."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from . import stage3_models as wf
from .command_port_common import CommandRuleError


def install(port_cls: type) -> None:
    """Use task-wide author run lineage, not agent labels, for Verification admission."""

    original = port_cls._start
    if getattr(original, "_task_wide_verifier_independence_patch", False):
        return

    def patched(self, call, generation, binding, execution, task, operation):
        if call.arguments.get("kind") != "verification":
            return original(self, call, generation, binding, execution, task, operation)

        assert task is not None
        agent = str(call.arguments.get("agent", "")).strip()
        if operation is None or operation.lifecycle != "open":
            raise CommandRuleError(
                "OPEN_OPERATION_REQUIRED",
                "Verification start requires the existing open operation",
            )
        if operation.phase != "await_verification":
            raise CommandRuleError(
                "VERIFICATION_NOT_READY",
                "the existing operation is not awaiting Verification",
            )
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        self._assert_reclaim_successor_claimable(operation=operation, call=call)
        target_cycle = call.arguments.get("target_cycle_id")
        if target_cycle is not None:
            try:
                target_cycle_uuid = uuid.UUID(str(target_cycle))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_CYCLE_ID",
                    "target cycle identifier must be a UUID",
                    http_status=400,
                ) from exc
            if target_cycle_uuid != cycle.cycle_id:
                raise CommandRuleError(
                    "VERIFICATION_CYCLE_MISMATCH",
                    "Verification start does not target the current cycle",
                )
        attestation = str(call.arguments.get("independence_attestation", "")).strip()
        if not attestation:
            raise CommandRuleError(
                "INDEPENDENCE_ATTESTATION_REQUIRED",
                "Verification start requires independence_attestation",
                http_status=400,
            )

        conflicting = self.session.scalar(
            select(wf.OperationActorFact)
            .join(
                wf.WorkflowOperation,
                wf.WorkflowOperation.operation_id == wf.OperationActorFact.operation_id,
            )
            .where(
                wf.OperationActorFact.task_id == task.task_id,
                wf.OperationActorFact.actor_role == "author",
                wf.OperationActorFact.run_id == call.run_id,
                wf.WorkflowOperation.kind.in_(("initial", "change")),
            )
            .order_by(
                wf.OperationActorFact.recorded_at,
                wf.OperationActorFact.actor_attempt_sequence,
            )
            .limit(1)
        )
        if conflicting is not None:
            raise CommandRuleError(
                "VERIFIER_NOT_INDEPENDENT",
                "the author or material editor cannot verify this candidate",
                data={"conflicting_actor_fact_id": str(conflicting.actor_fact_id)},
            )

        sequence = self._next_actor_attempt_sequence(task.task_id)
        actor_fact = self.workflow.create_actor_fact(
            actor_fact_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation.operation_id,
            run_id=call.run_id,
            owner_id=call.owner_id,
            actor_role="verification",
            agent=agent,
            actor_attempt_sequence=sequence,
            recorded_at=call.now,
        )
        lease = self.workflow.acquire_actor_lease(
            lease_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation.operation_id,
            run_id=call.run_id,
            owner_id=call.owner_id,
            actor_role="verification",
            actor_attempt_sequence=sequence,
            issued_at=call.now,
            expires_at=call.now + self.lease_duration,
            verification_cycle_id=cycle.cycle_id,
        )
        operation.persisted_actions = ["inspect"]
        operation.operation_revision += 1
        step_sequence = self._next_step(operation.operation_id)
        self.session.add(
            wf.OperationStep(
                step_id=self.uuid_factory(),
                operation_id=operation.operation_id,
                step_name=f"verification-start-{step_sequence}",
                step_sequence=step_sequence,
                outcome="complete",
                command_execution_id=execution.execution_id,
                evidence={
                    "cycle_id": str(cycle.cycle_id),
                    "actor_fact_id": str(actor_fact.actor_fact_id),
                    "lease_id": str(lease.lease_id),
                    "independence_attestation": attestation,
                },
                occurred_at=call.now,
            )
        )
        return {
            "operation_id": str(operation.operation_id),
            "cycle_id": str(cycle.cycle_id),
            "lease_id": str(lease.lease_id),
            "phase": operation.phase,
            "independence_attestation": attestation,
        }

    patched._task_wide_verifier_independence_patch = True
    port_cls._start = patched
