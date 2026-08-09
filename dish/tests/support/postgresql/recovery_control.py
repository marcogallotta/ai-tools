"""Shared recovery-control scenario builders for focused PostgreSQL tests."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.database import session_scope
from dish_pg.recovery_control import RecoveredPhysicalState, RestoreControl
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.transition import ProjectionService
from tests.support.postgresql.core import (
    NOW,
    _bootstrap_registry,
    _import_one,
    _next,
    _uuid_stream,
)


@pytest.fixture
def recovery_db(tmp_path):
    """File-backed recovery baseline with immutable generation provenance at schema head."""
    path = tmp_path / "recovery-control.sqlite3"
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA journal_mode = WAL")
        dbapi_connection.execute("PRAGMA busy_timeout = 30000")

    models.Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    ids = _uuid_stream()
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        task = _import_one(session, ids, context)
    yield factory, ids, context, task.task_id
    engine.dispose()


def _physical_state(**overrides) -> RecoveredPhysicalState:
    values = {
        "database_name": "dish_section2_restore_1",
        "system_identifier": "7600000000000000000",
        "schema_head": ALEMBIC_HEAD,
        "backup_manifest_sha256": "a" * 64,
        "backup_evidence_sha256": "b" * 64,
        "recovery_timeline_id": 3,
        "recovery_target_type": "lsn",
        "recovery_target_lsn": "0/ABCDEF0",
        "recovery_completion_lsn": "0/ABCDEF0",
        "recovery_target_instance_sha256": "c" * 64,
    }
    values.update(overrides)
    return RecoveredPhysicalState(**values)


def _control(context, ids, state: RecoveredPhysicalState) -> RestoreControl:
    return RestoreControl(
        external_control_id="section2-control-001",
        predecessor_generation_id=context["generation_id"],
        generation_id=_next(ids),
        bootstrap_id=_next(ids),
        bootstrap_capability_digest=hashlib.sha256(b"section2-current-actor").digest(),
        expected_database_name=state.database_name,
        expected_system_identifier=state.system_identifier,
        schema_head=state.schema_head,
        dish_release="dish-42619b9",
        honest_release="honest-1",
        protocol_release="protocol-1",
        openapi_release="openapi-1",
        routing_release="routing-1",
        backup_manifest_sha256=state.backup_manifest_sha256,
        backup_evidence_sha256=state.backup_evidence_sha256,
        recovery_timeline_id=state.recovery_timeline_id,
        recovery_target_type=state.recovery_target_type,
        recovery_target_lsn=state.recovery_target_lsn,
        recovery_completion_lsn=state.recovery_completion_lsn,
        recovery_target_instance_sha256=state.recovery_target_instance_sha256,
        recovery_evidence_sha256=state.evidence_sha256,
        issued_at=NOW + timedelta(minutes=1),
    )


def _inject_synthetic_candidate_state(session, ids, context, epoch_id, *, status: str):
    """Inject candidate rows for impossible/restored-state tests only."""
    batch_id, baseline_id, candidate_id = _next(ids), _next(ids), _next(ids)
    session.add_all(
        [
            projection_models.SourceImportBatch(
                import_batch_id=batch_id,
                generation_id=context["generation_id"],
                import_run_id=context["import_run_id"],
                source_release="dish-42619b9",
                source_commit=str(candidate_id),
                source_database_sha256="a" * 64,
                source_sidecars={"fixture": "recovery-control"},
                ledger_through_commit="42619b9",
                expected_entities=1,
                imported_entities=1,
                status="complete",
                started_at=NOW,
                completed_at=NOW,
            ),
            projection_models.ShadowBaseline(
                shadow_baseline_id=baseline_id,
                generation_id=context["generation_id"],
                source_generation_identity=str(candidate_id),
                source_commit=str(candidate_id),
                baseline_sequence=(candidate_id.int % 1000000) + 1,
                status="closed",
                disqualification_reason=None,
                created_at=NOW,
                terminal_at=NOW,
            ),
        ]
    )
    candidate = release_models.ReleaseCandidate(
        candidate_id=candidate_id,
        generation_id=context["generation_id"],
        source_import_batch_id=batch_id,
        shadow_baseline_id=baseline_id,
        projection_epoch_id=epoch_id,
        source_release="dish-42619b9",
        source_commit=str(candidate_id),
        ledger_through_commit="42619b9",
        schema_head=ALEMBIC_HEAD,
        dish_release="dish-42619b9",
        honest_release="honest-1",
        protocol_release="protocol-1",
        openapi_release="openapi-1",
        routing_release="routing-1",
        status="assembling",
        candidate_revision=1,
        validation_bundle_sha256=None,
        created_at=NOW,
        validated_at=None,
        approved_at=None,
        terminal_at=None,
    )
    session.add(candidate)
    session.flush()
    if status == "aborted":
        candidate.status = "aborted"
        candidate.candidate_revision = 2
        candidate.terminal_at = NOW
        session.flush()
    elif status == "activated":
        candidate.status = "validated"
        candidate.candidate_revision = 2
        candidate.validation_bundle_sha256 = "c" * 64
        candidate.validated_at = NOW
        session.flush()
        candidate.status = "approved"
        candidate.candidate_revision = 3
        candidate.approved_at = NOW
        session.flush()
        candidate.status = "activated"
        candidate.candidate_revision = 4
        candidate.terminal_at = NOW
        session.flush()
    return candidate


def _setup_synthetic_recovery_state(session, ids, *, candidate_status="assembling"):
    """Build synthetic recovery state; never use as proof of legitimate authority."""
    context = _bootstrap_registry(
        session,
        ids,
        generation_status="active",
        schema_head=ALEMBIC_HEAD,
    )
    epoch = ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
        generation_id=context["generation_id"],
        activation_reason="pre-restore live epoch",
        created_at=NOW,
        external_effects_enabled=True,
    )
    candidate = _inject_synthetic_candidate_state(
        session, ids, context, epoch.projection_epoch_id, status=candidate_status
    )
    session.flush()
    return context, epoch, candidate
