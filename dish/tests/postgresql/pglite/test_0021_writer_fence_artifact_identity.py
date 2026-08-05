"""Database-boundary coverage for writer-fence artifact identity."""
from __future__ import annotations

import importlib

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from dish_pg import artifact_identity_models as artifact
from dish_pg import stage6_models as rel
from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next

from tests.support.postgresql.pglite_fixtures import upgrade_on

pytestmark = pytest.mark.pglite






def _seed_candidate(session: Session):
    ids = _uuid_stream()
    context = _bootstrap_registry(session, ids, generation_status="active")
    task = _import_one(session, ids, context)
    _service, candidate_id = _prepare_candidate(session, ids, context, task.task_id)
    return ids, candidate_id


def _seed_fence(session: Session, *, result: str):
    ids, candidate_id = _seed_candidate(session)
    fence = rel.LegacyWriterFence(
        fence_id=_next(ids), candidate_id=candidate_id, target_identity="legacy-db",
        mechanism="permission-deny", manifest_sha256=HASH_A, state="prepared",
        fence_revision=1, proof_sha256=None, prepared_at=NOW, engaged_at=None,
        verified_at=None, released_at=None, artifact_observation_id=None,
        artifact_verification_result=None,
    )
    session.add(fence)
    session.flush()
    observation = artifact.WriterFenceArtifactObservation(
        observation_id=_next(ids), fence_id=fence.fence_id, candidate_id=candidate_id,
        artifact_generation_identity="cutover-generation-1",
        canonical_path="/srv/dish/legacy.sqlite3", content_sha256="b" * 64,
        filesystem_device=12, filesystem_inode=34, file_type="regular",
        regular_file=True, verification_result=result,
        observation_contract_version="writer-fence-v1", observed_at=NOW,
        recorded_at=NOW, evidence_sha256="c" * 64,
    )
    session.add(observation)
    session.flush()
    return fence.fence_id, observation.observation_id


def test_0021_mismatched_artifact_cannot_engage_fence(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    fence_id, observation_id = _seed_fence(session, result="mismatched")
            raw = connection.connection.driver_connection
            raw.autocommit = True
            with pytest.raises(psycopg.errors.CheckViolation):
                raw.execute(
                    """UPDATE legacy_writer_fences
                          SET state='engaged', fence_revision=2, engaged_at=%s,
                              artifact_observation_id=%s,
                              artifact_verification_result='mismatched'
                        WHERE fence_id=%s""",
                    (NOW, observation_id, fence_id),
                )
    finally:
        engine.dispose()


def test_0021_matched_artifact_engages_and_observation_is_immutable(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head")
            connection.commit()
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                with session.begin():
                    fence_id, observation_id = _seed_fence(session, result="matched")
            raw = connection.connection.driver_connection
            raw.autocommit = True
            raw.execute(
                """UPDATE legacy_writer_fences
                      SET state='engaged', fence_revision=2, engaged_at=%s,
                          artifact_observation_id=%s,
                          artifact_verification_result='matched'
                    WHERE fence_id=%s""",
                (NOW, observation_id, fence_id),
            )
            assert raw.execute(
                "SELECT state, artifact_verification_result FROM legacy_writer_fences WHERE fence_id=%s",
                (fence_id,),
            ).fetchone() == ("engaged", "matched")
            with pytest.raises(psycopg.errors.RaiseException, match="immutable writer-fence artifact observation"):
                raw.execute(
                    "UPDATE writer_fence_artifact_observations SET content_sha256=%s WHERE observation_id=%s",
                    ("e" * 64, observation_id),
                )
    finally:
        engine.dispose()


def test_0021_upgrade_refuses_abstract_engaged_predecessor_fence(pglite) -> None:
    engine = create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(
                connection,
                pglite.sqlalchemy_url,
                "0020_first_request_reservation",
            )
            connection.commit()
            raw = connection.connection.driver_connection
            raw.autocommit = True
            fence_id = __import__("uuid").uuid4()
            candidate_id = __import__("uuid").uuid4()
            raw.execute("SET session_replication_role = replica")
            raw.execute(
                """INSERT INTO legacy_writer_fences (
                    fence_id, candidate_id, target_identity, mechanism,
                    manifest_sha256, state, fence_revision, proof_sha256,
                    prepared_at, engaged_at, verified_at, released_at
                ) VALUES (%s,%s,'legacy-db','permission-deny',%s,'engaged',2,
                          NULL,%s,%s,NULL,NULL)""",
                (fence_id, candidate_id, HASH_A, NOW, NOW),
            )
            raw.execute("SET session_replication_role = origin")
            raw.autocommit = False
            migration = importlib.import_module(
                "dish_pg.migrations.versions.0021_writer_fence_artifact_identity"
            )
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="engaged writer fences"):
                    migration.upgrade()
    finally:
        engine.dispose()
