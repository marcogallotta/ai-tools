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
        mutation_id="replacement-previous-run-claim",
        target="dish_tool/database.py",
        before='if clean_run == str(row["previous_run_id"] or "").strip():',
        after="if False:",
        tests=(
            "tests/test_abandonment_stage_successors.py::test_prepared_planning_claim_rejects_abandoned_run_then_binds_fresh_run",
            "tests/test_safe_reclaim_workflow.py::test_different_run_can_safe_reclaim_clean_expired_verification_attempt",
        ),
        invariant="a replaced run cannot claim the successor attempt created to replace it",
    ),
    MutationCase(
        mutation_id="planning-intent-single-use",
        target="dish_service/planning_intent.py",
        before='if row["claimed_request_id"] == request_id and row["status"] in {',
        after='if row["status"] in {',
        tests=(
            "tests/test_planning_intent_confirmation.py::test_confirmation_challenge_is_single_use",
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
    MutationCase(
        mutation_id="planning-intent-owner-binding",
        target="dish_service/planning_intent.py",
        before='row["owner_id"] != principal.owner_id',
        after="False",
        tests=(
            "tests/test_planning_intent_confirmation.py::test_confirmation_rejects_different_owner_with_same_run",
        ),
        invariant="Planning intent confirmation remains bound to the authenticated owner independently of run identity",
    ),
    MutationCase(
        mutation_id="planning-intent-target-hash",
        target="dish_service/planning_intent.py",
        before='or row["target_hash"] != planning_intent_target_hash(arguments)',
        after="or False",
        tests=(
            "tests/test_planning_intent_confirmation.py::test_confirmation_rejects_hash_only_prepared_operation_change",
        ),
        invariant="Planning intent confirmation binds target arguments not covered by principal or task fields",
    ),
    MutationCase(
        mutation_id="workflow-policy-unresolved-effect",
        target="dish_tool/workflow_policy.py",
        before="or snapshot.unresolved_attempts",
        after="or False",
        tests=(
            "tests/test_workflow_policy_fail_closed.py::test_each_unsafe_authority_fact_suppresses_all_actions",
        ),
        invariant="unresolved external effects suppress every ordinary workflow action",
    ),
    MutationCase(
        mutation_id="workflow-policy-migration-reconciliation",
        target="dish_tool/workflow_policy.py",
        before="or snapshot.migration_reconciliation_required",
        after="or False",
        tests=(
            "tests/test_workflow_policy_fail_closed.py::test_each_unsafe_authority_fact_suppresses_all_actions",
        ),
        invariant="required migration reconciliation suppresses ordinary workflow actions",
    ),
    MutationCase(
        mutation_id="workflow-policy-placement-binding",
        target="dish_tool/workflow_policy.py",
        before="or not snapshot.placement_matches",
        after="or False",
        tests=(
            "tests/test_workflow_policy_fail_closed.py::test_each_unsafe_authority_fact_suppresses_all_actions",
        ),
        invariant="live Cooking-project placement must match the authoritative snapshot",
    ),
    MutationCase(
        mutation_id="workflow-policy-required-cycle",
        target="dish_tool/workflow_policy.py",
        before="if not snapshot.required_cycle_exists:",
        after="if False:",
        tests=(
            "tests/test_workflow_policy_fail_closed.py::test_each_unsafe_authority_fact_suppresses_all_actions",
        ),
        invariant="verification actions require the expected current verification cycle",
    ),
    MutationCase(
        mutation_id="workflow-policy-verification-live-state",
        target="dish_tool/workflow_policy.py",
        before='snapshot.live_status != "pending-verification"',
        after="False",
        tests=(
            "tests/test_workflow_policy_fail_closed.py::test_each_unsafe_authority_fact_suppresses_all_actions",
        ),
        invariant="verification actions require the live pending-verification state",
    ),
    MutationCase(
        mutation_id="workflow-policy-signoff-binding",
        target="dish_tool/workflow_policy.py",
        before='if snapshot.live_status != "ready" or not snapshot.signoff_bound:',
        after='if snapshot.live_status != "ready" or False:',
        tests=(
            "tests/test_workflow_policy_fail_closed.py::test_each_unsafe_authority_fact_suppresses_all_actions",
        ),
        invariant="submission actions require exact durable signoff binding",
    ),
    MutationCase(
        mutation_id="resting-change-signoff-binding",
        target="dish_tool/workflow_policy.py",
        before='if snapshot.canonical_status == "ready" and snapshot.signed_baseline_bound:',
        after='if snapshot.canonical_status == "ready":',
        tests=(
            "tests/test_change_start_intent.py::test_read_exposes_change_only_for_exact_signed_ready_resting_task",
        ),
        invariant="resting ready text exposes Change only when exact durable signoff is bound",
    ),
    MutationCase(
        mutation_id="change-creation-signoff-binding",
        target="dish_tool/database.py",
        before='if operation_kind == "change" and resolve_signoff_cycle_for_identity(\n            conn, task_gid=task_gid, identity=expected_identity\n        ) is None:',
        after='if False:',
        tests=(
            "tests/test_change_start_intent.py::test_direct_create_change_requires_signed_baseline_before_insert",
        ),
        invariant="direct Change operation creation requires exact durable signoff lineage before insert",
    ),
    MutationCase(
        mutation_id="task-store-cooking-project-selection",
        target="dish_tool/task_store.py",
        before="if membership_project_gid == project_gid:",
        after="if True:",
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_task_reader_selects_only_the_cooking_membership",
        ),
        invariant="task placement is selected by Cooking project identity rather than membership order",
    ),
    MutationCase(
        mutation_id="task-store-ambiguous-cooking-membership",
        target="dish_tool/task_store.py",
        before="if len(matches) > 1:",
        after="if False:",
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_task_reader_rejects_distinct_cooking_memberships",
        ),
        invariant="multiple distinct Cooking placements are rejected as ambiguous",
    ),
    MutationCase(
        mutation_id="backend-reread-task-identity",
        target="dish_tool/backend.py",
        before="if actual_task_gid == expected_task_gid:",
        after="if True:",
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_move_rejects_wrong_identity_after_sending",
        ),
        invariant="post-mutation confirmation must identify the exact requested task",
    ),
    MutationCase(
        mutation_id="backend-create-placement-confirmation",
        target="dish_tool/backend.py",
        before=(
            'if self._section_for_project(\n'
            '            confirmed, project_gid, partial_application="task_created"\n'
            '        ) != section_gid:'
        ),
        after="if False:",
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_create_rejects_task_outside_research_queue",
        ),
        invariant="created tasks must be reread in the requested Research Queue placement",
    ),
    MutationCase(
        mutation_id="backend-move-placement-confirmation",
        target="dish_tool/backend.py",
        before=(
            'if self._section_for_project(\n'
            '            after, COOKING_PROJECT_GID, partial_application="section_move_requested"\n'
            '        ) != section_gid:'
        ),
        after="if False:",
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_move_requires_requested_cooking_section_on_reread",
        ),
        invariant="section movement must be confirmed in the Cooking project after the effect",
    ),
    MutationCase(
        mutation_id="backend-move-content-drift",
        target="dish_tool/backend.py",
        before=(
            'if str(after.get("name") or "") != str(before.get("name") or "") '
            'or str(after.get("notes") or "") != str(before.get("notes") or ""):'
        ),
        after="if False:",
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_move_rejects_concurrent_content_change",
        ),
        invariant="movement confirmation rejects concurrent title or notes drift",
    ),
    MutationCase(
        mutation_id="backend-content-update-response-identity",
        target="dish_tool/backend.py",
        before=(
            'if response_gid != task_gid:\n'
            '            raise BackendFailure(\n'
            '                "BACKEND_UNCERTAIN",\n'
            '                "Asana returned malformed data after the title-and-notes write",'
        ),
        after=(
            'if False:\n'
            '            raise BackendFailure(\n'
            '                "BACKEND_UNCERTAIN",\n'
            '                "Asana returned malformed data after the title-and-notes write",'
        ),
        tests=(
            "tests/test_task_store_and_backend_negative_contracts.py::test_update_rejects_wrong_response_identity",
        ),
        invariant="content updates reject a response for a different task identity",
    ),

)

STAGE_A_CASES = (
    MutationCase(
        mutation_id="stage-a-strict-evidence-sha256",
        target="dish_pg/release_evidence.py",
        before='_SHA256_RE = re.compile(r"[0-9a-f]{64}\\Z")',
        after='_SHA256_RE = re.compile(r".{64}\\Z")',
        tests=(
            "tests/postgresql/test_release_evidence_contracts.py::test_release_evidence_requires_exact_lowercase_sha256",
        ),
        invariant="external release evidence accepts only exact lowercase hexadecimal SHA-256 values",
    ),
    MutationCase(
        mutation_id="stage-a-mandatory-projection-authority",
        target="dish_pg/command_port.py",
        before=(
            "self.projection_recorder: ProjectionAuthority = projection_recorder "
            "or ProjectionService(\n            session, uuid_factory=uuid_factory\n        )"
        ),
        after="self.projection_recorder = projection_recorder",
        tests=(
            "tests/postgresql/test_fail_closed_admission_outbox.py::test_default_command_port_uses_full_projection_authority",
        ),
        invariant="production command-port construction cannot create a projectionless mutation path",
    ),
    MutationCase(
        mutation_id="stage-a-writer-fence-auth-failure",
        target="dish_pg/cutover_control.py",
        before='"http_status": 409,\n            "response_code": "CONFLICT",',
        after='"http_status": 401,\n            "response_code": "CONFLICT",',
        tests=(
            "tests/postgresql/test_stage8_cutover_evidence_gates.py::test_writer_fence_proof_is_candidate_bound_and_pre_body_parse",
        ),
        invariant="authentication failure cannot self-attest as proof that the authenticated legacy writer is fenced",
    ),
    MutationCase(
        mutation_id="stage-a-command-effect-verifier",
        target="dish_pg/command_effect_runtime.py",
        before="if projection_types != expected.projection_event_types:",
        after="if False:",
        tests=(
            "tests/postgresql/test_command_effect_authority.py::test_execution_rejects_and_rolls_back_missing_projection_intent",
        ),
        invariant="committed projection effects are checked against the authoritative command-effect specification",
    ),
)

CASES = CASES + STAGE_A_CASES

