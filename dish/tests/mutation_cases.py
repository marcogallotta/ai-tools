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
    ),    MutationCase(
        mutation_id="abandonment-abandoned-run-claim",
        target="dish_tool/database.py",
        before='if clean_run == str(row["abandoned_run_id"] or "").strip():',
        after="if False:",
        tests=(
            "tests/test_abandonment_stage_successors.py::test_prepared_planning_claim_rejects_abandoned_run_then_binds_fresh_run",
        ),
        invariant="an abandoned run cannot claim the successor attempt created to replace it",
    ),
    MutationCase(
        mutation_id="planning-intent-single-use",
        target="dish_service/planning_intent.py",
        before='if row["claimed_request_id"] == request_id and row["status"] in {',
        after='if row["status"] in {',
        tests=(
            "tests/test_planning_intent_confirmation.py::test_challenge_is_bound_to_exact_principal_task_and_single_followup",
        ),
        invariant="a Planning intent challenge is reusable only by its exact claimed request",
    ),
    MutationCase(
        mutation_id="strict-fake-task-identity",
        target="tests/support/asana_backend.py",
        before="        try:\n            return self._tasks[gid]\n        except KeyError as exc:\n            raise AssertionError(f\"unexpected task gid: {gid}\") from exc",
        after="        return self._tasks[self.task_gid]",
        tests=(
            "tests/test_support_asana_backend.py::test_stateful_asana_backend_rejects_unknown_task_identity",
        ),
        invariant="the canonical Asana fake must not alias an unknown task to its seeded task",
    ),
    MutationCase(
        mutation_id="partial-effect-write-attribution",
        target="dish_tool/operation_execution.py",
        before='"write_committed": classification["write_committed"],',
        after='"write_committed": False,',
        tests=(
            "tests/test_dish_critical_partial_recovery.py::test_partial_failures_report_request_scoped_durable_evidence",
        ),
        invariant="partial-effect diagnostics report whether the content write committed",
    ),
    MutationCase(
        mutation_id="restore-installs-candidate",
        target="dish_service/backup.py",
        before="os.replace(candidate_path, self.db_path)",
        after="candidate_path.unlink()",
        tests=(
            "tests/test_operational_recovery.py::test_backup_restore_preserves_open_signoff_lease_and_recovery_state",
        ),
        invariant="restore installs the validated candidate rather than retaining the mutated live database",
    ),
    MutationCase(
        mutation_id="destination-already-placed",
        target="dish_tool/step9.py",
        before="elif current == destination.gid:",
        after="elif False:",
        tests=(
            "tests/test_terminal_placement.py::test_completed_task_already_present_in_destination_matches",
        ),
        invariant="a task already at its approved destination is accepted without another movement",
    ),

)
