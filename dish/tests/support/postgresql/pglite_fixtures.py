"""Shared Alembic and predecessor-state helpers for PGlite boundary tests."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session
from dish_pg import reservation_models as reservations
from dish_pg import stage6_models as rel
from tests.support.postgresql.core import ROOT, _bootstrap_registry, _import_one, _uuid_stream
from tests.support.postgresql.release import HASH_A, _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next, _register_run

def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini")); config.set_main_option("sqlalchemy.url", url); return config

def upgrade_on(connection, url: str, revision: str = "head") -> None:
    config = alembic_config(url); config.attributes["connection"] = connection; command.upgrade(config, revision)

def insert_generation(connection, generation_id: uuid.UUID) -> None:
    connection.execute("""INSERT INTO authority_generations (generation_id,predecessor_generation_id,creation_reason,external_restore_control_id,schema_head,dish_release,status,created_at,retired_at) VALUES (%s,NULL,'initial_cutover',NULL,%s,'test-release','active',%s,NULL)""", (generation_id,"0018_projection_attempt_lifecycle",datetime.now(timezone.utc)))

def insert_run(connection, *, generation_id: uuid.UUID, run_id: uuid.UUID, owner_id: str, digest_byte: bytes) -> None:
    connection.execute("""INSERT INTO service_runs (run_id,generation_id,owner_id,agent,capability_digest,bootstrap_id,status,registered_at,retired_at) VALUES (%s,%s,%s,'service',%s,NULL,'active',%s,NULL)""", (run_id,generation_id,owner_id,digest_byte*32,datetime.now(timezone.utc)))

def insert_request(connection, *, generation_id: uuid.UUID, request_id: uuid.UUID, run_id: uuid.UUID, owner_id: str) -> None:
    connection.execute("""INSERT INTO service_requests (request_id,generation_id,run_id,owner_id,principal_class,command_name,canonical_payload_sha256,canonical_payload,protocol_release,dish_release,admitted_at) VALUES (%s,%s,%s,%s,'service','inspect',%s,'{}'::json,'protocol-test','dish-test',%s)""", (request_id,generation_id,run_id,owner_id,"a"*64,datetime.now(timezone.utc)))

def seed_open_reservation(session: Session):
    ids=_uuid_stream(); context=_bootstrap_registry(session,ids,generation_status="active"); task=_import_one(session,ids,context)
    _service,candidate_id=_prepare_candidate(session,ids,context,task.task_id)
    cutover_id=_next(ids); request_id=_next(ids); run_id=_next(ids); plan_id=_next(ids)
    _register_run(session,generation_id=context["generation_id"],run_id=run_id,owner="owner-1",agent="service")
    session.add(rel.CutoverRun(cutover_run_id=cutover_id,candidate_id=candidate_id,state="admission_open",state_revision=5,started_at=NOW,terminal_at=None))
    session.add(rel.FirstAdmissionPlan(plan_id=plan_id,cutover_run_id=cutover_id,request_id=request_id,command_name="start",task_id=task.task_id,expected_projection_events=1,payload={"task_id":str(task.task_id)},plan_sha256=HASH_A,recorded_at=NOW)); session.flush()
    payload_sha="b"*64
    session.add(reservations.FirstRequestReservation(reservation_id=_next(ids),plan_id=plan_id,cutover_run_id=cutover_id,candidate_id=candidate_id,generation_id=context["generation_id"],request_id=request_id,command_name="start",owner_id="owner-1",principal_class="service",run_id=run_id,canonical_payload_sha256=payload_sha,state="reserved",reservation_revision=1,reserved_at=NOW,consumed_at=None))
    control=session.get(rel.MutationAdmissionControl,context["generation_id"]); assert control is not None and control.state=="closed"; session.flush()
    return context,request_id,run_id,payload_sha
