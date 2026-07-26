"""Transactional workflow facts; compatibility wrapper around existing persistence."""
from __future__ import annotations

from .database import (
    complete_operation_step,
    create_verification_cycle,
    declare_operation_step,
    legal_operation_actions,
    pending_operation_steps,
    record_actor_fact,
    transition_operation,
)


class WorkflowRepository:
    def __init__(self, conn) -> None:
        self.conn = conn

    def operation(self, operation_id):
        return self.conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()

    def legal_actions(self, operation):
        return legal_operation_actions(operation)

    def declare_step(self, operation_id, name, intended):
        return declare_operation_step(self.conn, operation_id, name, intended)

    def complete_step(self, operation_id, name):
        return complete_operation_step(self.conn, operation_id, name)

    def pending_steps(self, operation_id):
        return pending_operation_steps(self.conn, operation_id)

    def record_actor(self, **kwargs):
        return record_actor_fact(self.conn, **kwargs)

    def transition(self, operation_id, **kwargs):
        return transition_operation(self.conn, operation_id, **kwargs)
