"""Database-boundary coverage for typed worker readiness evidence."""
from __future__ import annotations

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dish_pg import readiness_evidence_models as typed
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from tests.support.postgresql.core import ROOT,_bootstrap_registry,_import_one,_uuid_stream
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.workflow import NOW,_next

from tests.support.postgresql.pglite_fixtures import upgrade_on

pytestmark=pytest.mark.pglite




def _seed(session,ids):
    context=_bootstrap_registry(session,ids,generation_status="active"); task=_import_one(session,ids,context)
    _service,candidate_id=_prepare_candidate(session,ids,context,task.task_id)
    candidate=session.get(rel.ReleaseCandidate,candidate_id); assert candidate is not None
    reconciliation=session.scalar(select(tx.ProjectionReconciliationRun).where(tx.ProjectionReconciliationRun.projection_epoch_id==candidate.projection_epoch_id)); assert reconciliation is not None
    inventory=typed.WorkerProbeInventory(inventory_id=_next(ids),candidate_id=candidate_id,projection_epoch_id=candidate.projection_epoch_id,inventory_version=1,required_probe_count=3,inventory_sha256='a'*64,inventory_contract_version='worker-probes-v1',sealed_at=NOW)
    session.add(inventory); session.flush()
    requirements=[]
    for ordinal,kind in enumerate(('claim','write','restart')):
        row=typed.WorkerProbeRequirement(requirement_id=_next(ids),inventory_id=inventory.inventory_id,probe_kind=kind,ordinal=ordinal,probe_contract_version='probe-v1'); session.add(row); requirements.append(row)
    session.flush()
    readiness=rel.ProjectionWorkerReadiness(readiness_id=_next(ids),candidate_id=candidate_id,projection_epoch_id=candidate.projection_epoch_id,reconciliation_run_id=reconciliation.reconciliation_run_id,probe_inventory_id=inventory.inventory_id,worker_identity='worker@artifact',worker_release=candidate.dish_release,payload={'claim_probe':'pass','write_probe':'pass','restart_probe':'pass'},readiness_sha256='b'*64,ready_at=NOW)
    session.add(readiness); session.flush()
    return candidate,inventory,requirements,readiness


def test_0026_pass_strings_without_probe_evidence_cannot_complete(pglite):
    engine=create_engine(pglite.sqlalchemy_url,future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "0026_typed_worker_readiness_evidence"); connection.commit(); ids=_uuid_stream()
            with Session(bind=connection,autoflush=False,expire_on_commit=False) as session:
                with session.begin(): candidate,inventory,_requirements,readiness=_seed(session,ids)
            raw=connection.connection.driver_connection; raw.autocommit=True
            with pytest.raises(psycopg.errors.RaiseException,match="not exactly complete"):
                raw.execute("""INSERT INTO worker_readiness_completions
                (completion_id,readiness_id,inventory_id,candidate_id,projection_epoch_id,completion_state,required_probe_count,passed_probe_count,completion_sha256,completed_at)
                VALUES (%s,%s,%s,%s,%s,'complete',3,3,%s,%s)""",(_next(ids),readiness.readiness_id,inventory.inventory_id,candidate.candidate_id,candidate.projection_epoch_id,'c'*64,NOW))
    finally: engine.dispose()


def test_0026_exact_typed_probe_inventory_can_complete(pglite):
    engine=create_engine(pglite.sqlalchemy_url,future=True)
    try:
        with engine.connect() as connection:
            upgrade_on(connection, pglite.sqlalchemy_url, "0026_typed_worker_readiness_evidence"); connection.commit(); ids=_uuid_stream()
            with Session(bind=connection,autoflush=False,expire_on_commit=False) as session:
                with session.begin():
                    candidate,inventory,requirements,readiness=_seed(session,ids)
                    for requirement in requirements:
                        session.add(typed.WorkerProbeEvidence(evidence_id=_next(ids),readiness_id=readiness.readiness_id,requirement_id=requirement.requirement_id,inventory_id=inventory.inventory_id,candidate_id=candidate.candidate_id,projection_epoch_id=candidate.projection_epoch_id,probe_kind=requirement.probe_kind,execution_identity=f'rehearsal:{requirement.probe_kind}',worker_identity=readiness.worker_identity,deployed_artifact_sha256='d'*64,result='pass',observed_at=NOW,evidence_artifact_identity=f'/evidence/{requirement.probe_kind}.json',evidence_sha256='e'*64,recorded_at=NOW))
                    session.flush()
                    completion=typed.WorkerReadinessCompletion(completion_id=_next(ids),readiness_id=readiness.readiness_id,inventory_id=inventory.inventory_id,candidate_id=candidate.candidate_id,projection_epoch_id=candidate.projection_epoch_id,completion_state='complete',required_probe_count=3,passed_probe_count=3,completion_sha256='f'*64,completed_at=NOW)
                    session.add(completion); session.flush()
                    assert completion.completion_state=='complete'
    finally: engine.dispose()
