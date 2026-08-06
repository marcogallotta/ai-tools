"""Native PostgreSQL certification diagnostics shared by pytest and lane scripts."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

DEFAULT_POSTGRESQL_DSN = (
    "postgresql+psycopg://dish:dish@127.0.0.1:55432/dish_stage_a_test"
)
NATIVE_POSTGRESQL_UNAVAILABLE = (
    "native PostgreSQL unavailable: rerun with --postgresql and an isolated "
    "DISH_TEST_POSTGRESQL_DSN; SQLite and PGlite are not certification substitutes"
)
_NON_NATIVE_SERVER_TOKENS = ("pglite", "emscripten", "webassembly", "wasm32")


class NativePostgreSQLUnavailable(RuntimeError):
    """The configured target is unreachable or is not a native PostgreSQL server."""


@dataclass(frozen=True)
class NativePostgreSQLIdentity:
    dialect: str
    driver: str
    database: str
    server_version: str
    server_version_full: str
    server_address: str | None
    server_port: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def postgresql_dsn() -> str:
    return os.environ.get("DISH_TEST_POSTGRESQL_DSN", DEFAULT_POSTGRESQL_DSN)


def redacted_dsn(dsn: str) -> str:
    """Return a report-safe DSN without credentials or query secrets."""

    url = make_url(dsn)
    return url.render_as_string(hide_password=True)


def probe_native_postgresql(dsn: str | None = None) -> NativePostgreSQLIdentity:
    """Connect once and prove the target is native PostgreSQL, not SQLite/PGlite."""

    target = dsn or postgresql_dsn()
    engine = create_engine(target, future=True, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            raise NativePostgreSQLUnavailable(
                f"configured dialect is {engine.dialect.name!r}, expected 'postgresql'"
            )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT current_database() AS database,
                           current_setting('server_version') AS server_version,
                           version() AS server_version_full,
                           inet_server_addr()::text AS server_address,
                           inet_server_port() AS server_port
                    """
                )
            ).mappings().one()
        full = str(row["server_version_full"])
        lowered = full.lower()
        token = next((value for value in _NON_NATIVE_SERVER_TOKENS if value in lowered), None)
        if token is not None:
            raise NativePostgreSQLUnavailable(
                f"server identity contains non-native token {token!r}: {full}"
            )
        return NativePostgreSQLIdentity(
            dialect=engine.dialect.name,
            driver=engine.dialect.driver,
            database=str(row["database"]),
            server_version=str(row["server_version"]),
            server_version_full=full,
            server_address=(
                None if row["server_address"] is None else str(row["server_address"])
            ),
            server_port=(None if row["server_port"] is None else int(row["server_port"])),
        )
    except NativePostgreSQLUnavailable:
        raise
    except Exception as exc:  # connection/driver failures are environment diagnostics
        raise NativePostgreSQLUnavailable(
            f"could not establish native PostgreSQL identity: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        engine.dispose()

# This inventory is intentionally literal. The native certification script rejects
# collection drift instead of deriving the required set from production or pytest.
NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY = (
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_cancelled_reservation_fails_closed",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_candidate_dependencies_must_match_generation",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_direct_sql_cannot_open_general_admission_before_verification",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_exact_first_request_fails_before_first_request_gate",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_exact_reserved_first_request_succeeds",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_first_request_replay_succeeds",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_initial_state_insert_guards_reject_direct_sql",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_mismatched_request_before_consumption_fails",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_missing_control_row_fails_closed",
    "tests/postgresql/native/test_first_request_reservation_single_gate.py::test_native_unrelated_valid_second_request_fails_before_verification",
    "tests/postgresql/native/test_importer.py::test_importer_persists_real_records_against_real_postgresql",
    "tests/postgresql/native/test_native_honest_binding_populated_migration.py::test_native_postgresql_populated_honest_binding_upgrade_enforces_identity",
    "tests/postgresql/native/test_native_honest_binding_populated_migration.py::test_native_postgresql_populated_honest_binding_upgrade_rejects_conflicts",
    "tests/postgresql/native/test_native_populated_migrations.py::test_native_postgresql_rejects_mismatched_cutover_candidate_lineage",
    "tests/postgresql/native/test_native_populated_migrations.py::test_native_postgresql_rejects_unverified_open_admission_predecessor",
    "tests/postgresql/native/test_native_populated_migrations.py::test_native_postgresql_upgrades_matching_cutover_candidate_lineage",
    "tests/postgresql/native/test_native_populated_migrations.py::test_native_postgresql_upgrades_populated_projection_attempt_predecessor",
    "tests/postgresql/native/test_operation_discard_prepare_concurrency.py::test_native_discard_commits_before_prepare_lock_and_leaves_no_actionable_intent",
    "tests/postgresql/native/test_operation_discard_prepare_concurrency.py::test_native_prepare_commits_before_discard_lock_and_discard_cannot_cancel",
    "tests/postgresql/native/test_process_failure_command.py::test_command_process_commit_before_response_replays_without_duplicate_mutation",
    "tests/postgresql/native/test_process_failure_command.py::test_command_process_disconnect_before_commit_fails_closed_and_recovers",
    "tests/postgresql/native/test_process_failure_disconnect.py::test_projection_worker_fails_clearly_across_postgresql_disconnect",
    "tests/postgresql/native/test_process_failure_disconnect.py::test_reconciliation_worker_writes_nothing_while_postgresql_is_down",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_ambiguous_external_response",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_claim_before_durable_intent",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_durable_intent_before_external_call",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_settlement_before_shutdown",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_before_claim",
    "tests/postgresql/native/test_process_failure_reconciliation.py::test_reconciliation_process_loss_after_durable_run_creation_resumes_exact_run",
    "tests/postgresql/native/test_process_failure_reconciliation.py::test_reconciliation_process_loss_after_partial_corpus_resumes_without_duplicate_items",
    "tests/postgresql/native/test_process_failure_supervision.py::test_long_running_projection_worker_is_supervised_and_restarted",
    "tests/postgresql/native/test_process_failure_supervision.py::test_reconciliation_worker_is_supervised_and_restarted",
    "tests/postgresql/native/test_process_failure_takeover.py::test_process_takeover_is_lease_gated_fenced_and_task_local",
    "tests/postgresql/native/test_production_shaped_runtime.py::test_section4_service_process_loss_replays_without_duplicate_effects",
    "tests/postgresql/native/test_production_shaped_runtime.py::test_section4_service_database_disconnect_rolls_back_then_recovers_once",
    "tests/postgresql/native/test_projection_epoch_lifecycle_concurrency.py::test_native_disable_during_candidate_selection_prevents_claim",
    "tests/postgresql/native/test_projection_epoch_lifecycle_concurrency.py::test_native_disable_after_claim_blocks_durable_dispatch_attempt",
    "tests/postgresql/native/test_projection_epoch_lifecycle_concurrency.py::test_native_event_insertion_admitted_before_retirement_is_superseded",
    "tests/postgresql/native/test_projection_epoch_lifecycle_concurrency.py::test_native_confirmed_settlement_waiting_before_retirement_is_preserved",
    "tests/postgresql/native/test_projection_attempt_concurrency.py::test_native_stale_settlement_races_current_owner_and_cannot_change_terminal_state",
    "tests/postgresql/native/test_projection_attempt_concurrency.py::test_native_worker_restart_observes_without_second_dispatch",
    "tests/postgresql/native/test_projection_worker.py::test_projection_worker_drains_one_pending_event_against_real_postgresql",
    "tests/postgresql/native/test_projection_worker.py::test_projection_worker_never_claims_real_shadow_evaluator_outbox",
    "tests/postgresql/native/test_reconciliation_worker.py::test_reconciliation_worker_completes_one_corpus_against_real_postgresql",
    "tests/postgresql/native/test_validation_replay.py::test_native_validation_failure_replays_one_authoritative_outcome",
    "tests/postgresql/native/test_validation_replay.py::test_native_concurrent_identical_validation_failures_converge",
    "tests/postgresql/native/test_validation_replay.py::test_native_closed_admission_preserves_first_request_reservation",
    "tests/postgresql/native/test_runtime_wiring_rehearsal.py::test_runtime_wiring_rehearsal_across_service_and_worker_processes",
    "tests/postgresql/native/test_shadow_baseline_concurrency.py::test_native_admitted_capture_blocks_close_and_forces_close_to_reread",
    "tests/postgresql/native/test_shadow_baseline_concurrency.py::test_native_committed_close_rejects_waiting_capture",
    "tests/postgresql/native/test_shadow_baseline_concurrency.py::test_native_committed_disqualification_rejects_waiting_capture",
    "tests/postgresql/native/test_shadow_baseline_concurrency.py::test_native_committed_disqualification_rejects_waiting_delivery_claim",
    "tests/postgresql/native/test_stage_a_concurrency.py::test_independent_tasks_acquire_authority_without_global_serialization",
    "tests/postgresql/native/test_stage_a_concurrency.py::test_ten_simultaneous_actor_lease_acquisitions_have_one_winner",
    "tests/postgresql/native/test_stage_a_concurrency.py::test_ten_simultaneous_duplicate_request_admissions_perform_one_logical_execution",
    "tests/postgresql/native/test_stage_a_concurrency.py::test_ten_simultaneous_marco_reservations_have_one_winner",
)
