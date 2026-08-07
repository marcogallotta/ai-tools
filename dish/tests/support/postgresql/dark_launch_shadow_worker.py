from __future__ import annotations
import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import (
    ShadowIdentityMappingError,
    ShadowWorker,
    _translate_workflow_identifiers,
)
from dish_pg.transition import ShadowService
from dish_pg.workflow import sha256_json
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.command import (
    _add_verification_queue,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

class Evaluator:
    def evaluate(self, session, envelope):
        del session
        return dict(envelope.source_outcome)

def _spool(tmp_path, *, treatment="execute", command_name="prepare"):
    spool=ShadowSpool(tmp_path/"spool.sqlite3")
    reservation=spool.reserve(
        source_request_identity=f"request-{command_name}-{treatment}", source_authority_generation="legacy-1",
        command_name=command_name, treatment=treatment,
        canonical_input={"command":command_name,"arguments":{}}, principal={},
        source_pre_state={"phase":"research"}, pinned_inputs={"rollout_mode":"execute"}, created_at=NOW,
    )
    spool.complete(reservation.registration_id, source_outcome={"ok":True},
                   source_post_state={"phase":"verification"}, source_effects={}, completed_at=NOW)
    return spool

def _real_verification_target(session, ids, context, task_id):
    _add_verification_queue(session, ids, context)
    author_run = _next(ids)
    verifier_run = _next(ids)
    _register_run(session, generation_id=context["generation_id"], run_id=author_run)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=verifier_run,
        owner="verifier-owner",
        agent="codex",
    )
    port = _port(session, ids)
    started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
    _prepare_for_verification(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=author_run,
    )

    savepoint = session.begin_nested()
    verification = _start_verification(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=verifier_run,
    )
    operation_id = verification.data["operation_id"]
    cycle_id = verification.data["cycle_id"]
    savepoint.rollback()
    session.expire_all()
    return operation_id, cycle_id
