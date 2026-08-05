from __future__ import annotations
import io
import json
import runpy
from datetime import timedelta
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import (
    ALEMBIC_HEAD,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    ReleaseAuthorityError,
    ReleaseCandidateService,
    sha256_json,
)
from dish_pg.workflow import (
    MutationAdmissionClosed,
    RequestSpec,
    WorkflowAuthorityService,
)
from dish_service.legacy_writer_fence import (
    engage_legacy_writer_fence,
    observe_legacy_writer_fence,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import (
    HASH_A,
    ROOT,
    _complete_active_mapping_reconciliation,
    _record_runtime_and_typed_readiness,
    _seed_worker_probe_inventory,
    _artifact_file,
    _prepare_candidate,
    _record_and_engage_writer_fence,
    _record_final_closure,
    _writer_fence_proof,
)

from tests.support.postgresql.stage8_cutover_evidence_gates import (
    _burn_rollback,
    _record_runtime_and_worker_readiness,
    _prepare_fenced_recertified_cutover,
)
from tests.support.postgresql.stage8_cutover_evidence_gates import _case_test_admission_requires_post_burn_runtime_worker_and_first_request_evidence


def test_admission_requires_post_burn_runtime_worker_and_first_request_evidence(workflow_db) -> None:
    return _case_test_admission_requires_post_burn_runtime_worker_and_first_request_evidence(workflow_db)

def test_stage8_operator_cli_exposes_readiness_and_first_admission_commands() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts" / "dish-pg-release"))
    parser = namespace["_parser"]()
    assert parser.parse_args([
        "runtime-attestation-record",
        "00000000-0000-0000-0000-000000000001",
        "--file",
        "/tmp/runtime.json",
    ]).command == "runtime-attestation-record"
    assert parser.parse_args([
        "projection-worker-ready",
        "00000000-0000-0000-0000-000000000001",
        "--file",
        "/tmp/worker.json",
    ]).command == "projection-worker-ready"
    assert parser.parse_args([
        "first-admission-plan",
        "00000000-0000-0000-0000-000000000002",
        "--file",
        "/tmp/first.json",
    ]).command == "first-admission-plan"

def test_first_admission_plan_rejects_unverifiable_target_shapes(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, _candidate_id, cutover_run_id = _burn_rollback(
            session, ids, context, task_id
        )
        first_run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=first_run_id)
        common = {
            "cutover_run_id": cutover_run_id,
            "request_id": _next(ids),
            "owner_id": "owner-1",
            "principal_class": "agent",
            "run_id": first_run_id,
            "payload": {"probe": "invalid-first-admission"},
            "recorded_at": NOW + timedelta(minutes=6),
        }
        with pytest.raises(ReleaseAuthorityError, match="must use the bounded start command"):
            service.plan_first_admission(
                **common,
                command_name="create",
                command_arguments={"title": "New task"},
                task_id=None,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="requires task_id"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
                task_id=None,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="must include canonical task_id"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={},
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="task identity conflicts"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={"task_id": str(_next(ids))},
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="must use the bounded start command"):
            service.plan_first_admission(
                **common,
                command_name="prepare",
                command_arguments={"task_id": str(task_id)},
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="cannot carry prior operation"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={
                    "task_id": str(task_id),
                    "agent": "codex",
                    "kind": "initial",
                    "operation_id": str(_next(ids)),
                },
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="kind must be initial"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={
                    "task_id": str(task_id),
                    "agent": "codex",
                    "kind": "planning",
                },
                task_id=task_id,
            )
        common["request_id"] = _next(ids)
        with pytest.raises(ReleaseAuthorityError, match="first-admission agent"):
            service.plan_first_admission(
                **common,
                command_name="start",
                command_arguments={"task_id": str(task_id), "kind": "initial"},
                task_id=task_id,
            )
