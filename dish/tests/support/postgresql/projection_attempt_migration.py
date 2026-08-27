"""Historical 0017 -> 0018 populated-predecessor migration fixture."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import IntegrityError
from tests.support.postgresql.migrations import MigrationDatabase

PREDECESSOR_REVISION = "0017_abandonment_terminal_state"
TARGET_REVISION = "0018_projection_attempt_lifecycle"
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
REQUEST_HASH = "a" * 64


@dataclass(frozen=True)
class ProjectionAttemptSeed:
    attempt_id: uuid.UUID
    event_id: uuid.UUID
    expected_dispatch_identity: str


def _attempt_table(connection) -> Table:
    return Table("projection_attempts", MetaData(), autoload_with=connection)


def _table(connection, name: str) -> Table:
    return Table(name, MetaData(), autoload_with=connection)


def _database_uuid(database: MigrationDatabase, value: uuid.UUID | None):
    if value is None or database.expected_dialect == "postgresql":
        return value
    return value.hex


def seed_valid_projection_attempt_predecessor(
    database: MigrationDatabase,
) -> ProjectionAttemptSeed:
    """Seed a predecessor-valid outbox event and legacy projection attempt."""

    values = iter(uuid.UUID(int=value) for value in range(1, 20))
    import_run_id = next(values)
    generation_id = next(values)
    task_id = next(values)
    binding_id = next(values)
    content_id = next(values)
    activation_id = next(values)
    epoch_id = next(values)
    event_id = next(values)
    attempt_id = next(values)
    dbid = lambda value: _database_uuid(database, value)

    def _seed(connection) -> None:
        connection.execute(
            _table(connection, "stage_a_import_runs").insert(),
            {
                "import_run_id": dbid(import_run_id),
                "source_commit": "1" * 40,
                "source_release": "historical-fixture",
                "legacy_generation_id": "projection-attempt-migration",
                "baseline_high_water_mark": "fixture",
                "source_bundle_sha256": "2" * 64,
                "status": "complete",
                "started_at": NOW,
                "completed_at": NOW,
                "provenance": {"fixture": "0017"},
            },
        )
        connection.execute(
            _table(connection, "authority_generations").insert(),
            {
                "generation_id": dbid(generation_id),
                "predecessor_generation_id": None,
                "creation_reason": "initial_cutover",
                "external_restore_control_id": None,
                "schema_head": PREDECESSOR_REVISION,
                "dish_release": "historical-fixture",
                "status": "active",
                "created_at": NOW,
                "retired_at": None,
            },
        )
        connection.execute(
            _table(connection, "honest_contract_bindings").insert(),
            {
                "binding_id": dbid(binding_id),
                "binding_kind": "release",
                "source_identity": "historical-fixture",
                "dish_release": "historical-fixture",
                "honest_release": "historical-fixture",
                "protocol_release": "historical-fixture",
                "protocol_sha256": "3" * 64,
                "schema_release": "historical-fixture",
                "schema_sha256": "4" * 64,
                "migration_id": None,
                "source_schema_version": None,
                "target_schema_version": None,
                "migration_metadata_sha256": None,
                "source_ids": {"fixture": "0017"},
                "provenance": {"fixture": "0017"},
                "resolved_at": NOW,
            },
        )
        connection.execute(
            _table(connection, "dish_tasks").insert(),
            {
                "task_id": dbid(task_id),
                "existence_state": "ordinary",
                "creation_route": "import",
                "import_run_id": dbid(import_run_id),
                "command_execution_id": None,
                "created_at": NOW,
                "retired_at": None,
            },
        )
        connection.execute(
            _table(connection, "task_content_versions").insert(),
            {
                "content_version_id": dbid(content_id),
                "generation_id": dbid(generation_id),
                "task_id": dbid(task_id),
                "representation_kind": "document",
                "title": "Historical fixture",
                "body": "body",
                "identity_scheme": "fixture",
                "content_identity": "fixture-v1",
                "creator_route": "import",
                "import_run_id": dbid(import_run_id),
                "command_execution_id": None,
                "predecessor_content_version_id": None,
                "contract_binding_id": dbid(binding_id),
                "created_at": NOW,
            },
        )
        connection.execute(
            _table(connection, "task_content_activations").insert(),
            {
                "content_activation_id": dbid(activation_id),
                "generation_id": dbid(generation_id),
                "task_id": dbid(task_id),
                "content_version_id": dbid(content_id),
                "activation_route": "import",
                "import_run_id": dbid(import_run_id),
                "command_execution_id": None,
                "task_revision": 1,
                "activated_at": NOW,
            },
        )
        connection.execute(
            _table(connection, "task_authority_heads").insert(),
            {
                "generation_id": dbid(generation_id),
                "task_id": dbid(task_id),
                "current_content_activation_id": dbid(activation_id),
                "task_revision": 1,
                "membership_revision": 0,
                "placement_revision": 0,
                "completion_revision": 1,
                "updated_at": NOW,
            },
        )
        epoch_values = {
            "projection_epoch_id": dbid(epoch_id),
            "generation_id": dbid(generation_id),
            "epoch_number": 1,
            "status": "active",
            "activation_reason": "populated predecessor migration test",
            "created_at": NOW,
            "retired_at": None,
        }
        epoch_table = _table(connection, "projection_epochs")
        if "external_effects_enabled" in epoch_table.c:
            epoch_values["external_effects_enabled"] = False
        connection.execute(epoch_table.insert(), epoch_values)
        connection.execute(
            _table(connection, "projection_outbox_events").insert(),
            {
                "projection_event_id": dbid(event_id),
                "generation_id": dbid(generation_id),
                "projection_epoch_id": dbid(epoch_id),
                "source_route": "service",
                "origin": "live",
                "command_execution_id": None,
                "task_id": dbid(task_id),
                "event_type": "update_task_document",
                "aggregate_sequence": 1,
                "idempotency_key": "5" * 64,
                "intent_payload": {"notes": "v2"},
                "intent_sha256": "6" * 64,
                "state": "pending",
                "claim_owner": None,
                "claim_token": None,
                "claim_expires_at": None,
                "outbox_revision": 1,
                "created_at": NOW,
                "terminal_at": None,
            },
        )
        connection.execute(
            _attempt_table(connection).insert(),
            {
                "attempt_id": dbid(attempt_id),
                "projection_event_id": dbid(event_id),
                "attempt_number": 1,
                "worker_id": "legacy-worker",
                "request_identity": "stable-logical-request",
                "intended_external_id": "123456789",
                "request_payload": {"notes": "v2"},
                "request_sha256": REQUEST_HASH,
                "state": "not_applied",
                "started_at": NOW,
                "terminal_at": NOW,
            },
        )

    database.seed(_seed)
    return ProjectionAttemptSeed(
        attempt_id=attempt_id,
        event_id=event_id,
        expected_dispatch_identity=attempt_id.hex + REQUEST_HASH[:32],
    )


def assert_projection_attempt_backfill(
    database: MigrationDatabase, seed: ProjectionAttemptSeed
) -> None:
    def _read(connection):
        table = _attempt_table(connection)
        return connection.execute(
            select(
                table.c.attempt_kind,
                table.c.predecessor_attempt_id,
                table.c.dispatch_identity,
                table.c.retry_generation,
                table.c.dispatch_claim_token,
                table.c.dispatch_claim_revision,
                table.c.request_identity,
                table.c.state,
            ).where(table.c.attempt_id == _database_uuid(database, seed.attempt_id))
        ).mappings().one()

    migrated = database.read(_read)
    assert migrated["attempt_kind"] == "dispatch"
    assert migrated["predecessor_attempt_id"] is None
    assert migrated["dispatch_identity"] == seed.expected_dispatch_identity
    assert migrated["retry_generation"] == 1
    assert migrated["dispatch_claim_token"] is None
    assert migrated["dispatch_claim_revision"] is None
    assert migrated["request_identity"] == "stable-logical-request"
    assert migrated["state"] == "not_applied"


def _post_upgrade_values(
    database: MigrationDatabase,
    seed: ProjectionAttemptSeed,
    *,
    attempt_id: uuid.UUID,
    attempt_number: int,
    attempt_kind: str = "dispatch",
    predecessor_attempt_id: uuid.UUID | None = None,
    dispatch_identity: str,
    retry_generation: int,
) -> dict[str, object]:
    return {
        "attempt_id": _database_uuid(database, attempt_id),
        "projection_event_id": _database_uuid(database, seed.event_id),
        "attempt_number": attempt_number,
        "attempt_kind": attempt_kind,
        "predecessor_attempt_id": _database_uuid(database, predecessor_attempt_id),
        "worker_id": "constraint-probe",
        "request_identity": f"stable-logical-request-{attempt_number}",
        "dispatch_identity": dispatch_identity,
        "retry_generation": retry_generation,
        "dispatch_claim_token": None,
        "dispatch_claim_revision": None,
        "intended_external_id": "123456789",
        "request_payload": {"notes": "v2"},
        "request_sha256": REQUEST_HASH,
        "state": "not_applied",
        "started_at": NOW,
        "terminal_at": NOW,
    }


def assert_projection_attempt_constraints(
    database: MigrationDatabase, seed: ProjectionAttemptSeed
) -> None:
    valid_recovery = _post_upgrade_values(
        database,
        seed,
        attempt_id=uuid.uuid4(),
        attempt_number=2,
        attempt_kind="recovery",
        predecessor_attempt_id=seed.attempt_id,
        dispatch_identity="b" * 64,
        retry_generation=2,
    )
    database.seed(lambda connection: connection.execute(_attempt_table(connection).insert(), valid_recovery))

    probes = (
        _post_upgrade_values(
            database,
            seed,
            attempt_id=uuid.uuid4(),
            attempt_number=3,
            dispatch_identity=seed.expected_dispatch_identity,
            retry_generation=3,
        ),
        _post_upgrade_values(
            database,
            seed,
            attempt_id=uuid.uuid4(),
            attempt_number=4,
            dispatch_identity="c" * 64,
            retry_generation=0,
        ),
        _post_upgrade_values(
            database,
            seed,
            attempt_id=uuid.uuid4(),
            attempt_number=5,
            attempt_kind="recovery",
            predecessor_attempt_id=uuid.uuid4(),
            dispatch_identity="d" * 64,
            retry_generation=5,
        ),
    )
    for values in probes:
        try:
            database.seed(
                lambda connection, values=values: connection.execute(
                    _attempt_table(connection).insert(), values
                )
            )
        except IntegrityError:
            continue
        raise AssertionError(f"post-upgrade constraint accepted invalid row: {values}")


def assert_projection_attempt_constraints_present(database: MigrationDatabase) -> None:
    """Inspect PostgreSQL/PGlite catalog without deliberately aborting the TCP shim."""

    # PGlite currently exposes the unique and foreign-key catalog entries but
    # not the named CHECK entries emitted by PostgreSQL DDL. Native PostgreSQL
    # and SQLite probes execute all invalid-row checks separately.
    expected = {
        "uq_projection_attempts_dispatch_identity",
        "fk_projection_attempts_predecessor_attempt_id",
    }

    def _read(connection):
        rows = connection.execute(
            select(sa.text("conname")).select_from(sa.text("pg_constraint")).where(
                sa.text("conrelid = 'projection_attempts'::regclass")
            )
        ).scalars()
        return set(rows)

    import sqlalchemy as sa

    actual = database.read(_read)
    missing = expected - actual
    if missing:
        raise AssertionError(f"missing projection_attempt constraints: {sorted(missing)}")
