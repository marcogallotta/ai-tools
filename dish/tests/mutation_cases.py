"""Curated launch-critical mutation probes for the Dish test suite."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MutationCase:
    mutation_id: str
    target: str
    before: str
    after: str
    tests: tuple[str, ...]
    invariant: str


CASES = (
    MutationCase(
        mutation_id="request-replay-marker",
        target="dish_service/request_replay.py",
        before='result.setdefault("data", {})["request_replayed"] = True',
        after='result.setdefault("data", {})["request_replayed"] = False',
        tests=(
            "tests/test_request_replay_and_restore_durability.py::test_completed_start_request_replays_full_stored_result",
        ),
        invariant="exact request replay is visibly identified without repeating work",
    ),
    MutationCase(
        mutation_id="request-run-binding",
        target="dish_service/request_replay.py",
        before='or row["run_id"] != run_id',
        after="or False",
        tests=(
            "tests/test_planning_reopen_authority_and_migration.py::test_exact_replay_preserves_owner_and_run_binding",
        ),
        invariant="request identity remains bound to the originating run",
    ),
    MutationCase(
        mutation_id="lease-owner-run-conjunction",
        target="dish_service/leases.py",
        before='return row["owner_id"] == principal.owner_id and row["run_id"] == principal.run_id',
        after='return row["owner_id"] == principal.owner_id or row["run_id"] == principal.run_id',
        tests=(
            "tests/test_lease_authority.py::test_inspect_actions_are_principal_aware_and_read_only",
        ),
        invariant="both owner and run identity are required for lease authority",
    ),
    MutationCase(
        mutation_id="authorization-consumed-identity",
        target="dish_tool/database.py",
        before="(now, candidate_identity, operation_id, *authorization_ids),",
        after="(now, None, operation_id, *authorization_ids),",
        tests=(
            "tests/test_authorization_provenance.py::test_marco_authorizations_reserve_all_or_nothing",
        ),
        invariant="consumed authorization is bound to the exact candidate identity",
    ),
    MutationCase(
        mutation_id="verifier-lineage-rejection",
        target="dish_tool/database.py",
        before="if prior is not None:\n        raise DishRuleError(\"AGENT_MISMATCH\", \"verifier run is already part of the candidate lineage\"",
        after="if False:\n        raise DishRuleError(\"AGENT_MISMATCH\", \"verifier run is already part of the candidate lineage\"",
        tests=(
            "tests/test_dish_tool_step7_verification.py::test_constructor_cannot_verify",
        ),
        invariant="a constructor or material-editor run cannot become the verifier",
    ),
    MutationCase(
        mutation_id="terminal-completion-timestamp",
        target="dish_tool/database.py",
        before='utc_now() if next_status in {"completed", "cancelled"}',
        after='utc_now() if next_status in {"completed"}',
        tests=(
            "tests/test_operation_lifecycle.py::test_cancelled_transition_records_terminal_completion_time",
        ),
        invariant="cancelled operations receive terminal completion evidence",
    ),
)
