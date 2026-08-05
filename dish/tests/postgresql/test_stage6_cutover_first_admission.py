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


def _prepare_approved_cutover(factory, ids, context, task_id):
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id, bundle_kind="release_candidate", built_at=NOW
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        closure = _record_final_closure(
            service, ids, candidate_id, closed_through_at=NOW + timedelta(minutes=5)
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="Approve this exact candidate and evidence bundle.",
            approval_payload={
                "decision": "approved",
                "final_asana_closure_id": str(closure.closure_id),
                "final_asana_closure_sha256": closure.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=5),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@laptop",
            mechanism="fail-closed-file",
            manifest={"path": "/var/lib/dish/legacy-writer-fence.json"},
            prepared_at=NOW + timedelta(minutes=5),
        )
        cutover = service.prepare_cutover(
            candidate_id=candidate_id, started_at=NOW + timedelta(minutes=5)
        )
        return candidate_id, closure.closure_id, cutover.cutover_run_id, fence.fence_id


def _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id):
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        _record_and_engage_writer_fence(service, ids, fence_id=fence_id, engaged_at=NOW + timedelta(minutes=5))
        fence = service.writer_fence_status(fence_id)
        writer_inventory = {fence.target_identity}
        service.verify_writer_fence(
            fence_id=fence_id,
            proof=_writer_fence_proof(fence, candidate_id),
            verified_at=NOW + timedelta(minutes=5),
            required_writer_inventory=writer_inventory,
        )
        service.mark_fenced(
            cutover_run_id=cutover_id,
            recorded_at=NOW + timedelta(minutes=5),
            required_writer_inventory=writer_inventory,
        )
        service.recertify_candidate(
            candidate_id=candidate_id,
            closure_id=closure_id,
            approver="Marco",
            recertification_statement="final closure remains exact after fencing",
            payload={"result": "pass"},
            recertified_at=NOW + timedelta(minutes=5),
        )
        service.activate_authority(
            cutover_run_id=cutover_id,
            final_asana_closure_id=closure_id,
            activated_at=NOW + timedelta(minutes=5),
            required_writer_inventory=writer_inventory,
        )


def _assert_admission_closed(factory, ids, context, task_id):
    with pytest.raises(MutationAdmissionClosed, match="admission is closed"):
        with session_scope(factory) as session:
            run_id = _next(ids)
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids)).admit_request(
                RequestSpec(
                    request_id=_next(ids),
                    generation_id=context["generation_id"],
                    run_id=run_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    command_name="start",
                    canonical_payload={"task_id": str(task_id)},
                    protocol_release="protocol-1",
                    dish_release="dish-42619b9",
                    admitted_at=NOW + timedelta(minutes=5),
                )
            )


def _burn_and_open_admission(factory, ids, context, task_id, candidate_id, cutover_id):
    first_request_id = _next(ids)
    first_run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=first_run_id)
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        activation = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=6),
        )
        assert activation.rollback_burned_at is not None
        candidate = service.candidate_status(candidate_id)
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="projection-worker-readiness",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        _record_runtime_and_typed_readiness(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )
        service.plan_first_admission(
            cutover_run_id=cutover_id,
            request_id=first_request_id,
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            owner_id="owner-1",
            principal_class="agent",
            run_id=first_run_id,
            payload={"probe": "first production mutation"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        control = service.open_mutation_admission(
            cutover_run_id=cutover_id, opened_at=NOW + timedelta(minutes=7)
        )
        assert control.state == "closed"
    return first_request_id, first_run_id


def _record_committed_first_request(factory, ids, context, task_id, cutover_id, request_id, run_id):
    with session_scope(factory) as session:
        binding = session.scalar(
            select(models.HonestContractBinding).where(
                models.HonestContractBinding.binding_kind == "release",
                models.HonestContractBinding.protocol_sha256 == HASH_A,
                models.HonestContractBinding.schema_sha256 == "b" * 64,
                models.HonestContractBinding.migration_id.is_(None),
                models.HonestContractBinding.migration_metadata_sha256.is_(None),
            )
        )
        if binding is None:
            binding = models.HonestContractBinding(
                binding_id=_next(ids),
                binding_kind="release",
                source_identity="honest-pantry@stage6-first-admission",
                dish_release="dish-pg-stage6",
                honest_release="honest-1",
                protocol_release="protocol-1",
                protocol_sha256=HASH_A,
                schema_release="schema-1",
                schema_sha256="b" * 64,
                migration_id=None,
                source_schema_version=None,
                target_schema_version=None,
                migration_metadata_sha256=None,
                source_ids={"route": "first-admission"},
                provenance={"source": "cutover-test"},
                resolved_at=NOW + timedelta(minutes=8),
            )
            session.add(binding)
            session.flush()
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        workflow.admit_request(
            RequestSpec(
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner_id="owner-1",
                principal_class="agent",
                command_name="start",
                canonical_payload={
                    "command": "start",
                    "arguments": {"task_id": str(task_id), "agent": "codex", "kind": "initial"},
                    "owner_id": "owner-1",
                    "run_id": str(run_id),
                },
                protocol_release=binding.protocol_release,
                dish_release=binding.dish_release,
                admitted_at=NOW + timedelta(minutes=8),
            )
        )
        execution_id = _next(ids)
        workflow.begin_execution(
            ExecutionSpec(
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=None,
                command_name="start",
                transaction_profile="L",
                canonical_intent={"command": "start", "arguments": {"task_id": str(task_id), "agent": "codex", "kind": "initial"}},
                pinned_inputs={"now": (NOW + timedelta(minutes=8)).isoformat()},
                contract_binding_id=binding.binding_id,
                admitted_at=NOW + timedelta(minutes=8),
            )
        )
        workflow.repo.record_outcome(
            request_id=request_id,
            outcome=StoredOutcome(
                outcome_id=_next(ids),
                outcome_class="success",
                result_code="OK",
                http_status=200,
                result_payload={"ok": True, "first_admission": True},
                immutable_success=True,
                recorded_at=NOW + timedelta(minutes=8),
            ),
            execution_id=execution_id,
            audit_event_id=_next(ids),
            audit_event_type="first_production_admission",
            actor="owner-1",
            audit_payload={"cutover_run_id": str(cutover_id)},
            task_id=task_id,
            operation_id=None,
            obligation_id=_next(ids),
            invocation_metadata={"surface": "production-cutover"},
        )
        obligation = session.scalar(
            select(wf.InvocationAuditObligation).where(
                wf.InvocationAuditObligation.request_id == request_id
            )
        )
        assert obligation is not None
        obligation.state = "fulfilled"
        obligation.terminal_at = NOW + timedelta(minutes=8)


def _verify_and_complete(factory, ids, context, candidate_id, cutover_id, request_id):
    with session_scope(factory) as session:
        _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity="post-first-admission",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        with pytest.raises(ReleaseAuthorityError, match="execution, audit, projection, and reconciliation"):
            service.verify_first_admission(
                cutover_run_id=cutover_id,
                request_id=request_id,
                verified_at=NOW + timedelta(minutes=8),
            )
        service.verify_first_admission(
            cutover_run_id=cutover_id,
            request_id=request_id,
            verified_at=NOW + timedelta(minutes=9),
        )
        control = session.get(rel.MutationAdmissionControl, context["generation_id"])
        assert control is not None
        assert control.state == "open"
        assert control.opened_at == (NOW + timedelta(minutes=9)).replace(tzinfo=None)
        completed = service.complete_cutover(
            cutover_run_id=cutover_id, completed_at=NOW + timedelta(minutes=10)
        )
        assert completed.state == "completed"
        final_bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="cutover_final",
            built_at=NOW + timedelta(minutes=10),
        )
        assert final_bundle.manifest["activation"] is not None
        assert final_bundle.manifest["acceptance"]["passed"], [
            check for check in final_bundle.manifest["acceptance"]["checks"] if not check["passed"]
        ]
        with pytest.raises(ReleaseAuthorityError, match="prohibited"):
            service.abort_cutover(
                cutover_run_id=cutover_id,
                reason="too late",
                aborted_at=NOW + timedelta(minutes=11),
            )


def test_cutover_is_resumable_admission_stays_closed_until_burn_and_first_outcome(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    _assert_admission_closed(factory, ids, context, task_id)
    request_id, run_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )
    _record_committed_first_request(
        factory, ids, context, task_id, cutover_id, request_id, run_id
    )
    _verify_and_complete(factory, ids, context, candidate_id, cutover_id, request_id)



@pytest.mark.parametrize("defect", ["stale", "wrong_candidate", "wrong_boundary"])
def test_first_admission_rejects_non_strict_post_request_reconciliation(
    workflow_db, defect: str
) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)
    request_id, run_id = _burn_and_open_admission(
        factory, ids, context, task_id, candidate_id, cutover_id
    )
    _record_committed_first_request(
        factory, ids, context, task_id, cutover_id, request_id, run_id
    )

    verified_at = NOW + timedelta(minutes=9)
    with session_scope(factory) as session:
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=context["generation_id"],
            corpus_identity=f"post-first-admission-{defect}",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        if defect == "stale":
            verified_at = NOW + timedelta(hours=2)
        elif defect == "wrong_candidate":
            candidate = session.get(rel.ReleaseCandidate, candidate_id)
            assert candidate is not None
            other = rel.ReleaseCandidate(
                candidate_id=_next(ids),
                generation_id=candidate.generation_id,
                source_import_batch_id=candidate.source_import_batch_id,
                shadow_baseline_id=candidate.shadow_baseline_id,
                projection_epoch_id=candidate.projection_epoch_id,
                source_release=candidate.source_release,
                source_commit="f" * 64,
                ledger_through_commit=candidate.ledger_through_commit,
                schema_head=candidate.schema_head,
                dish_release=candidate.dish_release,
                honest_release=candidate.honest_release,
                protocol_release=candidate.protocol_release,
                openapi_release=candidate.openapi_release,
                routing_release=candidate.routing_release,
                status="assembling",
                candidate_revision=1,
                validation_bundle_sha256=None,
                created_at=candidate.created_at,
                validated_at=None,
                approved_at=None,
                terminal_at=None,
            )
            session.add(other)
            session.flush()
            other.status = "activated"
            other.candidate_revision = 2
            other.validation_bundle_sha256 = candidate.validation_bundle_sha256
            other.validated_at = candidate.validated_at
            other.approved_at = candidate.approved_at
            other.terminal_at = candidate.terminal_at
            session.flush()
            reconciliation.candidate_id = other.candidate_id
        else:
            reconciliation.external_high_water = None
            reconciliation.external_snapshot_identity = "snapshot:wrong-contract"
        session.flush()

        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        with pytest.raises(
            ReleaseAuthorityError,
            match="execution, audit, projection, and reconciliation",
        ):
            service.verify_first_admission(
                cutover_run_id=cutover_id,
                request_id=request_id,
                verified_at=verified_at,
            )

def test_rollback_burn_replay_requires_exact_bundle_and_timestamp(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)

    burned_at = NOW + timedelta(minutes=6)
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        activation = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle:" + HASH_A,
            burned_at=burned_at,
        )
        replay = service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle:" + HASH_A,
            burned_at=burned_at,
        )
        assert replay.activation_id == activation.activation_id

        with pytest.raises(ReleaseAuthorityError, match="identity conflict"):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle:" + "b" * 64,
                burned_at=burned_at,
            )
        with pytest.raises(ReleaseAuthorityError, match="identity conflict"):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle:" + HASH_A,
                burned_at=burned_at + timedelta(seconds=1),
            )
        with pytest.raises(ReleaseAuthorityError, match="nonblank"):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="   ",
                burned_at=burned_at,
            )


@pytest.mark.database_boundary
def test_rollback_bundle_identity_migration_adds_nonblank_constraint(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    path = tmp_path / "rollback-bundle.sqlite3"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "head")

    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        checks = {
            row["name"]: row["sqltext"]
            for row in inspect(engine).get_check_constraints("authority_activations")
        }
        assert "ck_authority_activations_legacy_bundle_nonblank" in checks
        assert "trim(legacy_bundle_id)" in checks[
            "ck_authority_activations_legacy_bundle_nonblank"
        ]
        assert ALEMBIC_HEAD == "0029_cutover_authority_admission_fixes"
    finally:
        engine.dispose()


def test_consumed_first_reservation_blocks_second_request_until_verification(workflow_db) -> None:
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



def test_rollback_burn_rechecks_candidate_quiescence_immediately_before_burn(
    workflow_db, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    candidate_id, closure_id, cutover_id, fence_id = _prepare_approved_cutover(
        factory, ids, context, task_id
    )
    _activate_authority(factory, ids, candidate_id, closure_id, cutover_id, fence_id)

    calls: list[tuple[object, object]] = []
    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))

        ordering: list[str] = []
        monkeypatch.setattr(
            service,
            "_fence_rollback_burn_state",
            lambda: ordering.append("fence"),
        )

        def failed_evaluation(*, candidate_id, as_of):
            ordering.append("evaluate")
            calls.append((candidate_id, as_of))
            return CandidateEvaluation(
                candidate_id=candidate_id,
                checks=(
                    AcceptanceCheck(
                        "quiescent_cutover_authority",
                        False,
                        {"authority_operations": 1},
                    ),
                ),
            )

        monkeypatch.setattr(service, "evaluate_candidate", failed_evaluation)
        with pytest.raises(
            ReleaseAuthorityError,
            match="failed immediately before rollback burn: quiescent_cutover_authority",
        ):
            service.burn_rollback(
                cutover_run_id=cutover_id,
                legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
                burned_at=NOW + timedelta(minutes=6),
            )
        assert ordering == ["fence", "evaluate"]
        assert calls == [(candidate_id, NOW + timedelta(minutes=6))]
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"],
                models.AuthorityActivation.outcome == "activated",
            )
        )
        assert activation is None


def test_sqlite_direct_sql_initial_state_and_generation_guards(workflow_db) -> None:
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
