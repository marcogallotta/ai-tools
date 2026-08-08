from __future__ import annotations
from datetime import timedelta
import json
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD, ReleaseAuthorityError, ReleaseCandidateService
from dish_pg.release_status import AcceptanceCheck, CandidateEvaluation
from dish_pg.workflow import (
    ExecutionSpec,
    MutationAdmissionClosed,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityService,
    sha256_json,
)
from tests.support.postgresql.first_admission import (
    _prepare_approved_cutover,
    _activate_authority,
    _assert_admission_closed,
    _burn_and_open_admission,
    _record_committed_first_request,
    _verify_and_complete,
    open_verified_first_admission,
)
from tests.support.postgresql.release import (
    HASH_A,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _record_runtime_and_worker_readiness_report,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.stage6_cutover_first_admission import _case_test_consumed_first_reservation_blocks_second_request_until_verification
from tests.support.postgresql.stage6_cutover_first_admission import _case_test_sqlite_direct_sql_initial_state_and_generation_guards



def test_consumed_first_reservation_blocks_second_request_until_verification(workflow_db) -> None:
    return _case_test_consumed_first_reservation_blocks_second_request_until_verification(workflow_db)

def test_sqlite_direct_sql_initial_state_and_generation_guards(workflow_db) -> None:
    return _case_test_sqlite_direct_sql_initial_state_and_generation_guards(workflow_db)

def test_sqlite_direct_sql_missing_control_fails_closed(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        _service, _candidate_id = _prepare_candidate(session, ids, context, task_id)
        run_id = _next(ids)
        request_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        session.execute(
            text("DELETE FROM mutation_admission_controls WHERE generation_id = :generation_id"),
            {"generation_id": context["generation_id"].hex},
        )
        payload = {
            "command": "start",
            "arguments": {"task_id": str(task_id)},
        }
        with pytest.raises(IntegrityError, match="mutation admission is closed"):
            session.execute(
                text(
                    """INSERT INTO service_requests (
                        request_id,generation_id,run_id,owner_id,principal_class,
                        command_name,canonical_payload_sha256,canonical_payload,
                        protocol_release,dish_release,admitted_at
                    ) VALUES (
                        :request_id,:generation_id,:run_id,'owner-1','agent','start',
                        :payload_sha,:payload,'protocol-1','dish-pg-stage6',:admitted_at
                    )"""
                ),
                {
                    "request_id": request_id.hex,
                    "generation_id": context["generation_id"].hex,
                    "run_id": run_id.hex,
                    "payload_sha": sha256_json(payload),
                    "payload": json.dumps(payload),
                    "admitted_at": NOW,
                },
            )
