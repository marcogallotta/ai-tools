"""Explicit ownership for shared helpers that fabricate durable workflow state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowBuilderContract:
    classification: str
    rationale: str
    producer_equivalence_tests: tuple[str, ...] = ()


WORKFLOW_BUILDER_CONTRACTS = {
    "support/abandonment.py::_source": WorkflowBuilderContract(
        classification="workflow_state",
        rationale="Creates source operation shapes consumed by abandonment workflow tests.",
        producer_equivalence_tests=(
            "test_abandonment_frontiers.py::test_real_planning_prepare_crash_before_terminal_preserves_committed_finalized_route",
            "test_abandonment_stage_successors.py::test_real_reject_route_hold_then_abandonment_creates_research_successor",
        ),
    ),
    "support/abandonment.py::_abandon": WorkflowBuilderContract(
        classification="workflow_state",
        rationale="Creates the durable abandonment authority consumed by successor tests.",
        producer_equivalence_tests=(
            "test_abandonment_admin_workflow.py::test_admin_abandon_operation_creates_exact_planning_successor",
        ),
    ),
    "support/abandonment_scenarios.py::abandonment_in_state": WorkflowBuilderContract(
        classification="workflow_state",
        rationale="Composes source and abandonment builders into reachable frontier states.",
        producer_equivalence_tests=(
            "test_abandonment_frontiers.py::test_real_planning_prepare_crash_before_terminal_preserves_committed_finalized_route",
            "test_abandonment_stage_successors.py::test_real_reject_route_hold_then_abandonment_creates_research_successor",
            "test_abandonment_admin_workflow.py::test_admin_abandon_operation_creates_exact_planning_successor",
        ),
    ),
    "support/abandonment_scenarios.py::frontier_operation": WorkflowBuilderContract(
        classification="persistence_shape",
        rationale="Intentionally fabricates operation phases to isolate frontier persistence rules.",
    ),
    "support/abandonment_scenarios.py::frontier_abandonment": WorkflowBuilderContract(
        classification="persistence_shape",
        rationale="Intentionally creates exact lease and abandonment rows for persistence-boundary tests.",
    ),
    "support/abandonment_scenarios.py::persistence_source": WorkflowBuilderContract(
        classification="persistence_shape",
        rationale="Builds configurable operation, cycle, and lease rows for schema and trigger tests.",
    ),
    "support/abandonment_scenarios.py::start_abandonment": WorkflowBuilderContract(
        classification="persistence_shape",
        rationale="Exercises abandonment insertion constraints inside an explicit writer transaction.",
    ),
    "support/backend_service_resilience.py::_aba_operation": WorkflowBuilderContract(
        classification="persistence_shape",
        rationale=(
            "Creates one valid operation persistence shape for the backend-effect "
            "ABA recovery tests; those consumers isolate write-outcome evidence "
            "rather than asserting workflow producer equivalence."
        ),
    ),
    "support/semantic_evidence.py::insert_operation": WorkflowBuilderContract(
        classification="persistence_shape",
        rationale="Deliberately inserts incomplete operation evidence for semantic-validator diagnostics.",
    ),
}
