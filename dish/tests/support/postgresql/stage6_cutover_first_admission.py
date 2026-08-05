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
    _record_runtime_and_typed_readiness,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db




def _case_test_consumed_first_reservation_blocks_second_request_until_verification(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    first_request_id, first_run_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )
    _record_committed_first_request(
        factory, ids, context, task_id, cutover_id, first_request_id, first_run_id
    )

    with session_scope(factory) as session:
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        second_run_id = _next(ids)
        second_request_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=second_run_id,
        )
        with pytest.raises(
            MutationAdmissionClosed,
            match="pending first-admission verification",
        ):
            workflow.admit_request(
                RequestSpec(
                    request_id=second_request_id,
                    generation_id=context["generation_id"],
                    run_id=second_run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="start",
                    canonical_payload={
                        "command": "start",
                        "arguments": {
                            "task_id": str(task_id),
                            "agent": "codex",
                            "kind": "follow-up",
                        },
                        "owner_id": "owner-1",
                        "run_id": str(second_run_id),
                    },
                    protocol_release="protocol-1",
                    dish_release="dish-pg-stage6",
                    admitted_at=NOW + timedelta(minutes=9),
                )
            )

        accepted_first = session.get(wf.ServiceRequest, first_request_id)
        assert accepted_first is not None
        first_replay = workflow.admit_request(
            RequestSpec(
                request_id=accepted_first.request_id,
                generation_id=accepted_first.generation_id,
                run_id=accepted_first.run_id,
                owner_id=accepted_first.owner_id,
                principal_class=accepted_first.principal_class,
                command_name=accepted_first.command_name,
                canonical_payload=accepted_first.canonical_payload,
                protocol_release=accepted_first.protocol_release,
                dish_release=accepted_first.dish_release,
                admitted_at=NOW + timedelta(minutes=9),
            )
        )
        assert first_replay.replayed
        assert first_replay.outcome is not None
        assert first_replay.outcome.result_code == "OK"

        with pytest.raises(
            IntegrityError,
            match="mutation admission opens only after verified first admission",
        ), session.begin_nested():
            session.execute(
                text(
                    """UPDATE mutation_admission_controls
                       SET state = 'open',
                           control_revision = control_revision + 1,
                           opened_at = :opened_at,
                           updated_at = :opened_at
                     WHERE generation_id = :generation_id"""
                ),
                {
                    "generation_id": context["generation_id"].hex,
                    "opened_at": NOW + timedelta(minutes=9),
                },
            )
            session.flush()


    with session_scope(factory) as session:
        _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="post-first-admission-open-general",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        ReleaseCandidateService(session, uuid_factory=lambda: _next(ids)).verify_first_admission(
            cutover_run_id=cutover_id,
            request_id=first_request_id,
            verified_at=NOW + timedelta(minutes=9),
        )
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None and control.state == "open"

    with session_scope(factory) as session:
        second = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)).admit_request(
            RequestSpec(
                request_id=second_request_id,
                generation_id=context["generation_id"],
                run_id=second_run_id,
                owner_id="owner-1",
                principal_class="agent",
                command_name="start",
                canonical_payload={
                    "command": "start",
                    "arguments": {
                        "task_id": str(task_id),
                        "agent": "codex",
                        "kind": "follow-up",
                    },
                    "owner_id": "owner-1",
                    "run_id": str(second_run_id),
                },
                protocol_release="protocol-1",
                dish_release="dish-pg-stage6",
                admitted_at=NOW + timedelta(minutes=10),
            )
        )
        assert not second.replayed
        assert second.request.request_id == second_request_id


def _case_test_sqlite_direct_sql_initial_state_and_generation_guards(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _burn_and_open_admission(factory, ids, context, task_id, candidate_id, cutover_id)

    with session_scope(factory) as session:
        invalid_statements = (
            (
                "release candidate must initially be assembling",
                """INSERT INTO release_candidates (
                    candidate_id,generation_id,source_import_batch_id,shadow_baseline_id,
                    projection_epoch_id,source_release,source_commit,ledger_through_commit,
                    schema_head,dish_release,honest_release,protocol_release,openapi_release,
                    routing_release,status,candidate_revision,validation_bundle_sha256,
                    created_at,validated_at,approved_at,terminal_at
                )
                SELECT :new_id,generation_id,source_import_batch_id,shadow_baseline_id,
                       projection_epoch_id,source_release,source_commit,ledger_through_commit,
                       schema_head,dish_release,honest_release,protocol_release,openapi_release,
                       routing_release,'validated',1,validation_bundle_sha256,
                       created_at,validated_at,NULL,NULL
                  FROM release_candidates LIMIT 1""",
                {"new_id": _next(ids).hex},
            ),
            (
                "mutation admission control must initially be closed",
                """INSERT INTO mutation_admission_controls (
                    generation_id,candidate_id,state,control_revision,opened_at,updated_at
                )
                SELECT generation_id,candidate_id,'open',1,updated_at,updated_at
                  FROM mutation_admission_controls LIMIT 1""",
                {},
            ),
            (
                "first-request reservation must initially be reserved",
                """INSERT INTO first_request_reservations (
                    reservation_id,plan_id,cutover_run_id,candidate_id,generation_id,
                    request_id,command_name,owner_id,principal_class,run_id,
                    canonical_payload_sha256,state,reservation_revision,reserved_at,consumed_at
                )
                SELECT :new_id,plan_id,cutover_run_id,candidate_id,generation_id,
                       request_id,command_name,owner_id,principal_class,run_id,
                       canonical_payload_sha256,'cancelled',1,reserved_at,NULL
                  FROM first_request_reservations LIMIT 1""",
                {"new_id": _next(ids).hex},
            ),
            (
                "release candidate must initially be assembling",
                """INSERT INTO release_candidates (
                    candidate_id,generation_id,source_import_batch_id,shadow_baseline_id,
                    projection_epoch_id,source_release,source_commit,ledger_through_commit,
                    schema_head,dish_release,honest_release,protocol_release,openapi_release,
                    routing_release,status,candidate_revision,validation_bundle_sha256,
                    created_at,validated_at,approved_at,terminal_at
                )
                SELECT :new_id,generation_id,source_import_batch_id,shadow_baseline_id,
                       projection_epoch_id,source_release,source_commit,ledger_through_commit,
                       schema_head,dish_release,honest_release,protocol_release,openapi_release,
                       routing_release,'assembling',2,NULL,
                       created_at,NULL,NULL,NULL
                  FROM release_candidates LIMIT 1""",
                {"new_id": _next(ids).hex},
            ),
            (
                "mutation admission control must initially be closed",
                """INSERT INTO mutation_admission_controls (
                    generation_id,candidate_id,state,control_revision,opened_at,updated_at
                )
                SELECT generation_id,candidate_id,'closed',2,NULL,updated_at
                  FROM mutation_admission_controls LIMIT 1""",
                {},
            ),
            (
                "first-request reservation must initially be reserved",
                """INSERT INTO first_request_reservations (
                    reservation_id,plan_id,cutover_run_id,candidate_id,generation_id,
                    request_id,command_name,owner_id,principal_class,run_id,
                    canonical_payload_sha256,state,reservation_revision,reserved_at,consumed_at
                )
                SELECT :new_id,plan_id,cutover_run_id,candidate_id,generation_id,
                       request_id,command_name,owner_id,principal_class,run_id,
                       canonical_payload_sha256,'reserved',2,reserved_at,NULL
                  FROM first_request_reservations LIMIT 1""",
                {"new_id": _next(ids).hex},
            ),
        )
        for message, statement, params in invalid_statements:
            with pytest.raises(IntegrityError, match=message), session.begin_nested():
                session.execute(text(statement), params)
                session.flush()

        other_generation_id = _next(ids)
        session.add(
            models.AuthorityGeneration(
                generation_id=other_generation_id,
                predecessor_generation_id=None,
                creation_reason="initial_cutover",
                external_restore_control_id=None,
                schema_head=ALEMBIC_HEAD,
                dish_release="dish-pg-stage6",
                status="pending",
                created_at=NOW,
                retired_at=None,
            )
        )
        session.flush()
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"), session.begin_nested():
            session.execute(
                text(
                    """INSERT INTO release_candidates (
                        candidate_id,generation_id,source_import_batch_id,shadow_baseline_id,
                        projection_epoch_id,source_release,source_commit,ledger_through_commit,
                        schema_head,dish_release,honest_release,protocol_release,openapi_release,
                        routing_release,status,candidate_revision,validation_bundle_sha256,
                        created_at,validated_at,approved_at,terminal_at
                    )
                    SELECT :candidate_id,:generation_id,source_import_batch_id,shadow_baseline_id,
                           projection_epoch_id,source_release,source_commit,ledger_through_commit,
                           schema_head,dish_release,honest_release,protocol_release,openapi_release,
                           routing_release,'assembling',1,NULL,created_at,NULL,NULL,NULL
                      FROM release_candidates LIMIT 1"""
                ),
                {
                    "candidate_id": _next(ids).hex,
                    "generation_id": other_generation_id.hex,
                },
            )
            session.flush()
