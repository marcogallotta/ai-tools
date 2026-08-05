"""PostgreSQL boundary coverage for candidate approval manifest binding."""
from __future__ import annotations

import importlib
from datetime import timedelta

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import stage5_models as tx
from dish_pg.candidate_manifest import revalidate_candidate_manifest
from tests.support.postgresql.release import HASH_A, _prepare_candidate, _record_final_closure
from tests.support.postgresql.workflow import NOW, _next

from tests.support.postgresql.pglite_fixtures import upgrade_on

pytestmark = pytest.mark.pglite






def _validated_candidate(session: Session):
    ids = _uuid_stream()
    context = _bootstrap_registry(session, ids, generation_status="active")
    task = _import_one(session, ids, context)
    service, candidate_id = _prepare_candidate(session, ids, context, task.task_id)
    bundle = service.build_evidence_bundle(candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW)
    service.validate_candidate(candidate_id=candidate_id, evidence_bundle_id=bundle.bundle_id, validated_at=NOW + timedelta(minutes=1))
    return ids, candidate_id, bundle.bundle_id


def _insert_approval(raw, *, approval_id, candidate_id, bundle_id):
    raw.execute(
        """INSERT INTO cutover_approvals (
            approval_id, candidate_id, evidence_bundle_id, approver,
            approval_statement, approval_payload, approval_sha256, approved_at
        ) VALUES (%s,%s,%s,'Marco','exact approval','{}'::json,%s,%s)""",
        (approval_id, candidate_id, bundle_id, HASH_A, NOW + timedelta(minutes=2)),
    )


def test_0022_database_rejects_approval_without_manifest_binding(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    ids, candidate_id, bundle_id = _validated_candidate(session)
                    approval_id = _next(ids)
            raw = connection.connection.driver_connection; raw.autocommit = True
            _insert_approval(raw, approval_id=approval_id, candidate_id=candidate_id, bundle_id=bundle_id)
            with pytest.raises(psycopg.errors.RaiseException, match="lacks exact authority manifest binding"):
                raw.execute(
                    "UPDATE release_candidates SET status='approved', candidate_revision=candidate_revision+1, approved_at=%s WHERE candidate_id=%s",
                    (NOW + timedelta(minutes=2), candidate_id),
                )
    finally:
        engine.dispose()


def test_0022_upgrade_refuses_already_approved_predecessor_candidate(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(
                connection,
                pglite.sqlalchemy_url,
                "0021_writer_fence_artifact_identity",
            )
            connection.commit()
            raw = connection.connection.driver_connection
            raw.autocommit = True
            import uuid

            values = [uuid.uuid4() for _ in range(5)]
            raw.execute("SET session_replication_role = replica")
            raw.execute(
                """INSERT INTO release_candidates (
                    candidate_id,generation_id,source_import_batch_id,
                    shadow_baseline_id,projection_epoch_id,source_release,
                    source_commit,ledger_through_commit,schema_head,dish_release,
                    honest_release,protocol_release,openapi_release,routing_release,
                    status,candidate_revision,validation_bundle_sha256,created_at,
                    validated_at,approved_at,terminal_at
                ) VALUES (%s,%s,%s,%s,%s,'source',%s,%s,'0018',
                          'dish','honest','protocol','openapi','routing','approved',3,
                          %s,%s,%s,%s,NULL)""",
                (*values, "a" * 64, "b" * 64, HASH_A, NOW, NOW, NOW),
            )
            raw.execute("SET session_replication_role = origin")
            raw.autocommit = False
            migration = importlib.import_module(
                "dish_pg.migrations.versions.0022_candidate_state_manifest"
            )
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(
                    RuntimeError, match="already-approved predecessor candidates"
                ):
                    migration.upgrade()
    finally:
        engine.dispose()



def test_0022_database_rejects_false_matched_component_attestation(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    ids = _uuid_stream()
                    context = _bootstrap_registry(session, ids, generation_status="active")
                    task = _import_one(session, ids, context)
                    service, candidate_id = _prepare_candidate(
                        session, ids, context, task.task_id
                    )
                    bundle = service.build_evidence_bundle(
                        candidate_id=candidate_id,
                        bundle_kind="release_candidate",
                        built_at=NOW,
                    )
                    service.validate_candidate(
                        candidate_id=candidate_id,
                        evidence_bundle_id=bundle.bundle_id,
                        validated_at=NOW + timedelta(minutes=1),
                    )
                    closure = _record_final_closure(
                        service,
                        ids,
                        candidate_id,
                        closed_through_at=NOW + timedelta(minutes=2),
                    )
                    service.approve_candidate(
                        candidate_id=candidate_id,
                        evidence_bundle_id=bundle.bundle_id,
                        approver="Marco",
                        approval_statement="Approve exact authority manifest.",
                        approval_payload={
                            "final_asana_closure_id": str(closure.closure_id),
                            "final_asana_closure_sha256": closure.closure_sha256,
                        },
                        approved_at=closure.recorded_at,
                    )
                    manifest = session.scalar(
                        select(manifest_models.ReleaseCandidateManifest).where(
                            manifest_models.ReleaseCandidateManifest.candidate_id
                            == candidate_id
                        )
                    )
                    assert manifest is not None
                    manifest_values = {
                        "manifest_id": manifest.manifest_id,
                        "manifest_version": manifest.manifest_version,
                        "canonical_fingerprint": manifest.canonical_fingerprint,
                        "mapping": manifest.mapping_membership_sha256,
                        "import_completion": manifest.import_completion_sha256,
                        "typed_links": manifest.typed_import_linkage_sha256,
                        "reconciliation": manifest.reconciliation_evidence_sha256,
                        "readiness_inventory": manifest.readiness_inventory_sha256,
                        "readiness_completion": manifest.readiness_completion_sha256,
                    }
                    revalidation_id = _next(ids)
            raw = connection.connection.driver_connection
            raw.autocommit = True
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="matched candidate revalidation component digest mismatch",
            ):
                raw.execute(
                    """INSERT INTO candidate_manifest_revalidations (
                        revalidation_id, candidate_id, manifest_id, manifest_version,
                        approved_fingerprint, observed_fingerprint,
                        observed_mapping_membership_sha256,
                        observed_import_completion_sha256,
                        observed_typed_import_linkage_sha256,
                        observed_reconciliation_evidence_sha256,
                        observed_readiness_inventory_sha256,
                        observed_readiness_completion_sha256, result, revalidated_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'matched',%s
                    )""",
                    (
                        revalidation_id,
                        candidate_id,
                        manifest_values["manifest_id"],
                        manifest_values["manifest_version"],
                        manifest_values["canonical_fingerprint"],
                        manifest_values["canonical_fingerprint"],
                        "f" * 64,
                        manifest_values["import_completion"],
                        manifest_values["typed_links"],
                        manifest_values["reconciliation"],
                        manifest_values["readiness_inventory"],
                        manifest_values["readiness_completion"],
                        NOW + timedelta(minutes=3),
                    ),
                )
    finally:
        engine.dispose()

def test_0022_database_activation_rejects_latest_stale_component_revalidation(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    ids = _uuid_stream()
                    context = _bootstrap_registry(session, ids, generation_status="active")
                    task = _import_one(session, ids, context)
                    service, candidate_id = _prepare_candidate(session, ids, context, task.task_id)
                    bundle = service.build_evidence_bundle(
                        candidate_id=candidate_id,
                        bundle_kind="release_candidate",
                        built_at=NOW,
                    )
                    service.validate_candidate(
                        candidate_id=candidate_id,
                        evidence_bundle_id=bundle.bundle_id,
                        validated_at=NOW + timedelta(minutes=1),
                    )
                    closure = _record_final_closure(
                        service,
                        ids,
                        candidate_id,
                        closed_through_at=NOW + timedelta(minutes=2),
                    )
                    service.approve_candidate(
                        candidate_id=candidate_id,
                        evidence_bundle_id=bundle.bundle_id,
                        approver="Marco",
                        approval_statement="Approve exact authority manifest.",
                        approval_payload={
                            "final_asana_closure_id": str(closure.closure_id),
                            "final_asana_closure_sha256": closure.closure_sha256,
                        },
                        approved_at=closure.recorded_at,
                    )
                    candidate = service._candidate(candidate_id)
                    matched = revalidate_candidate_manifest(
                        session,
                        uuid_factory=lambda: _next(ids),
                        candidate=candidate,
                        revalidated_at=NOW + timedelta(minutes=3),
                    )
                    assert matched.result == "matched"
                    mapping = session.scalar(
                        select(tx.TaskProjectionMapping).where(
                            tx.TaskProjectionMapping.generation_id == candidate.generation_id,
                            tx.TaskProjectionMapping.projection_epoch_id
                            == candidate.projection_epoch_id,
                            tx.TaskProjectionMapping.state == "active",
                        )
                    )
                    assert mapping is not None
                    mapping.state = "retired"
                    mapping.mapping_revision += 1
                    mapping.retired_at = NOW + timedelta(minutes=4)
                    session.flush()
                    stale = revalidate_candidate_manifest(
                        session,
                        uuid_factory=lambda: _next(ids),
                        candidate=candidate,
                        revalidated_at=NOW + timedelta(minutes=5),
                    )
                    assert stale.result == "stale"
            raw = connection.connection.driver_connection
            raw.autocommit = True
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="lacks fresh matched manifest revalidation",
            ):
                raw.execute(
                    "UPDATE release_candidates "
                    "SET status='activated', candidate_revision=candidate_revision+1, terminal_at=%s "
                    "WHERE candidate_id=%s",
                    (NOW + timedelta(minutes=6), candidate_id),
                )
    finally:
        engine.dispose()
