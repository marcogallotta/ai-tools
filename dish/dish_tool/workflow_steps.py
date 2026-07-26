"""Immutable workflow-plan definitions and idempotent step metadata."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    intended: Mapping[str, object]
    external_effect: bool = False


@dataclass(frozen=True)
class WorkflowPlan:
    operation_id: str
    steps: tuple[WorkflowStep, ...]

    def declare(self, repository) -> None:
        for step in self.steps:
            repository.declare_step(self.operation_id, step.name, step.intended)
