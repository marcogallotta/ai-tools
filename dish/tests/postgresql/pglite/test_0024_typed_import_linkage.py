"""PostgreSQL boundary coverage for typed import linkage."""
from __future__ import annotations

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dish_pg import import_link_models as links
from dish_pg import stage5_models as tx
from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next

from tests.support.postgresql.pglite_fixtures import upgrade_on

pytestmark = pytest.mark.pglite




def test_0024_exact_typed_link_succeeds_and_contradictory_binding_fails(pglite) -> None:
    engine=create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "head"); connection.commit()
            ids=_uuid_stream()
            with Session(bind=connection,autoflush=False,expire_on_commit=False) as session:
                with session.begin():
                    context=_bootstrap_registry(session,ids,generation_status="active")
                    task=_import_one(session,ids,context)
                    _service,_candidate_id=_prepare_candidate(session,ids,context,task.task_id)
                    evidence=session.scalar(select(tx.SourceImportEntityEvidence).where(tx.SourceImportEntityEvidence.entity_kind=="task"))
                    assert evidence is not None
                    batch=session.get(tx.SourceImportBatch,evidence.import_batch_id); assert batch is not None
                    existing_link=session.scalar(
                        select(links.SourceImportNativeLink).where(
                            links.SourceImportNativeLink.evidence_id == evidence.evidence_id
                        )
                    )
                    assert existing_link is not None
                    assert existing_link.task_id == task.task_id
            raw=connection.connection.driver_connection; raw.autocommit=True
            with pytest.raises(psycopg.Error):
                raw.execute(
                    """INSERT INTO source_import_native_links
                    (link_id,evidence_id,import_batch_id,import_run_id,entity_kind,project_id,linked_at)
                    VALUES (%s,%s,%s,%s,'project',%s,%s)""",
                    (_next(ids),evidence.evidence_id,evidence.import_batch_id,batch.import_run_id,context["project_id"],NOW),
                )
    finally:
        engine.dispose()
