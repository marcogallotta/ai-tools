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

pytestmark = pytest.mark.pglite


def _upgrade_on(connection, url):
    c=Config(str(ROOT / "alembic.ini")); c.set_main_option("sqlalchemy.url", url); c.attributes["connection"]=connection
    command.upgrade(c, "head")


def test_0024_exact_typed_link_succeeds_and_contradictory_binding_fails(pglite) -> None:
    engine=create_engine(pglite.sqlalchemy_url, future=True)
    try:
        with engine.connect() as connection:
            _upgrade_on(connection,pglite.sqlalchemy_url); connection.commit()
            ids=_uuid_stream()
            with Session(bind=connection,autoflush=False,expire_on_commit=False) as session:
                with session.begin():
                    context=_bootstrap_registry(session,ids,generation_status="active")
                    task=_import_one(session,ids,context)
                    _service,_candidate_id=_prepare_candidate(session,ids,context,task.task_id)
                    evidence=session.scalar(select(tx.SourceImportEntityEvidence).where(tx.SourceImportEntityEvidence.entity_kind=="task"))
                    assert evidence is not None
                    batch=session.get(tx.SourceImportBatch,evidence.import_batch_id); assert batch is not None
                    link_id=_next(ids)
                    session.add(links.SourceImportNativeLink(
                        link_id=link_id,evidence_id=evidence.evidence_id,
                        import_batch_id=evidence.import_batch_id,import_run_id=batch.import_run_id,
                        entity_kind="task",task_id=task.task_id,project_id=None,section_id=None,
                        content_version_id=None,request_tombstone_id=None,linked_at=NOW,
                    ))
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
