"""PostgreSQL boundary tests for reconciliation freshness evidence."""
from __future__ import annotations

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.release import ALEMBIC_HEAD
from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next

from tests.support.postgresql.pglite_fixtures import upgrade_on

pytestmark=pytest.mark.pglite




def test_0025_complete_candidate_reconciliation_requires_exact_fresh_boundary(pglite):
    engine=create_engine(pglite.sqlalchemy_url,future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head"); connection.commit(); ids=_uuid_stream()
            with Session(bind=connection,autoflush=False,expire_on_commit=False) as session:
                with session.begin():
                    context=_bootstrap_registry(session,ids,generation_status="active",schema_head=ALEMBIC_HEAD); task=_import_one(session,ids,context)
                    _service,candidate_id=_prepare_candidate(session,ids,context,task.task_id)
                    candidate=session.get(__import__('dish_pg.stage6_models',fromlist=['ReleaseCandidate']).ReleaseCandidate,candidate_id)
                    active=session.get(models.ActiveSectionRegistry,context["generation_id"])
                    assert candidate is not None and active is not None
            raw=connection.connection.driver_connection; raw.autocommit=True
            with pytest.raises(psycopg.errors.CheckViolation):
                raw.execute(
                    """INSERT INTO projection_reconciliation_runs
                    (reconciliation_run_id,generation_id,projection_epoch_id,corpus_identity,status,
                     expected_items,processed_items,started_at,completed_at,candidate_id,registry_version_id,
                     observation_started_at,corpus_manifest_sha256,scope_complete,adapter_contract_version,evidence_recorded_at)
                    VALUES (%s,%s,%s,'corpus','complete',0,0,%s,%s,%s,%s,%s,%s,false,'adapter-v1',%s)""",
                    (_next(ids),context["generation_id"],candidate.projection_epoch_id,NOW,NOW,candidate_id,
                     active.registry_version_id,NOW,'a'*64,NOW),
                )
    finally:
        engine.dispose()
