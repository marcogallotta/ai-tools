"""Current-head populated predecessor case built on the generic migration harness."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _uuid_stream
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


def _database_uuid(database: MigrationDatabase, value: uuid.UUID | None):
    if value is None or database.expected_dialect == "postgresql":
        return value
    return value.hex


def seed_valid_projection_attempt_predecessor(
    database: MigrationDatabase,
) -> ProjectionAttemptSeed:
    """Seed a predecessor-valid outbox event and legacy projection attempt."""

    engine = database.create_engine()
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    ids = _uuid_stream()
    try:
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids, generation_status="active")
            task = _import_one(session, ids, context)
            projection = ProjectionService(session, uuid_factory=lambda: next(ids))
            projection.activate_epoch(
                generation_id=context["generation_id"],
                activation_reason="populated predecessor migration test",
                created_at=NOW,
                external_effects_enabled=False,
            )
            event = projection._record_event(
                generation_id=context["generation_id"],
                execution_id=None,
                task_id=task.task_id,
                event_type="update_task_document",
                payload={"notes": "v2"},
                source_route="service",
                origin="live",
                created_at=NOW,
            )
            attempt_id = next(ids)
            table = _attempt_table(session.connection())
            session.execute(
                table.insert().values(
                    attempt_id=_database_uuid(database, attempt_id),
                    projection_event_id=_database_uuid(database, event.projection_event_id),
                    attempt_number=1,
                    worker_id="legacy-worker",
                    request_identity="stable-logical-request",
                    intended_external_id="123456789",
                    request_payload={"notes": "v2"},
                    request_sha256=REQUEST_HASH,
                    state="not_applied",
                    started_at=NOW,
                    terminal_at=NOW,
                )
            )
        return ProjectionAttemptSeed(
            attempt_id=attempt_id,
            event_id=event.projection_event_id,
            expected_dispatch_identity=attempt_id.hex + REQUEST_HASH[:32],
        )
    finally:
        engine.dispose()


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
