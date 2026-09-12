from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import uuid

from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _port,
    _prepare_for_verification,
    _inspect,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import _next, _register_run


def test_ineligible_verification_caller_gets_handoff_and_replay(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        port = _port(session, ids)
        started = _start_initial(
            port, ids, task_id=task_id, run_id=author_run, agent="claude"
        )
        _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
            agent="claude",
        )

        request_id = _next(ids)
        same_run_call = _call(
            "start",
            run_id=author_run,
            request_id=request_id,
            arguments={
                "task_id": str(task_id),
                "kind": "verification",
                "agent": "codex",
                "independence_attestation": "independent",
            },
        )
        same_run = port.execute(same_run_call)
        replay = port.execute(same_run_call)

        assert same_run.code == "VERIFIER_NOT_INDEPENDENT"
        assert same_run.allowed_actions == ()
        assert same_run.data["verification_eligibility"] == {
            "eligible": False,
            "rule": "VERIFIER_NOT_INDEPENDENT",
            "conflicting_actor_fact_id": same_run.data["conflicting_actor_fact_id"],
        }
        assert same_run.data["verification_handoff"] == {
            "required": True,
            "requirement": "independent_verifier",
            "instruction": (
                "Hand this task to an independent caller. That caller must read the current "
                "task and follow its returned Verification continuation."
            ),
            "action_template": {
                "command": "read",
                "arguments": {"dish_id": str(task_id)},
                "required_caller_arguments": ["agent"],
            },
        }
        assert replay.request_replayed is True
        assert replay.code == same_run.code
        assert replay.data == same_run.data
        assert session.scalar(
            select(func.count()).select_from(wf.VerificationInspectionOccurrence)
        ) == 0


def test_verification_continuation_is_caller_aware_without_changing_raw_legality(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    verifier_run = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(
            port, ids, task_id=task_id, run_id=author_run, agent="claude"
        )
        prepared = _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
            agent="claude",
        )

        # Mutation responses keep workflow legality; caller-aware filtering is a read
        # projection, while a rejected start carries the same explicit handoff.
        assert prepared.allowed_actions == ("start",)
        assert prepared.data["required_start_kind"] == "verification"

        author_read = port.execute(
            _call(
                "read",
                run_id=author_run,
                arguments={"dish_id": str(task_id), "agent": "claude"},
            )
        )
        assert author_read.allowed_actions == ()
        assert author_read.data["legal_actions"] == ("verify",)
        assert author_read.data["verification_eligibility"]["eligible"] is False
        assert author_read.data["verification_handoff"]["requirement"] == "independent_verifier"
        assert "required_start_kind" not in author_read.data
        assert "agent_action" not in author_read.data

        verifier_read = port.execute(
            _call(
                "read",
                run_id=verifier_run,
                owner="verifier-owner",
                arguments={"dish_id": str(task_id), "agent": "claude"},
            )
        )
        assert verifier_read.allowed_actions == ("start",)
        assert verifier_read.data["legal_actions"] == ("verify",)
        assert verifier_read.data["required_start_kind"] == "verification"
        assert verifier_read.data["agent_action"] == {
            "command": "start",
            "arguments": {"dish_id": str(task_id), "kind": "verification"},
        }
        assert "verification_eligibility" not in verifier_read.data
        assert "verification_handoff" not in verifier_read.data

        verification = _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="claude",
        )
        assert verification.ok


def test_verification_occurrence_projection_and_reclaim_are_exact_caller_aware(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    verifier_run = _next(ids)
    foreign_run = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=foreign_run,
            owner="foreign-owner",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(
            port, ids, task_id=task_id, run_id=author_run, agent="claude"
        )
        _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
            agent="claude",
        )
        verification = _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )

        owner_before = port.execute(
            _call(
                "read",
                run_id=verifier_run,
                owner="verifier-owner",
                arguments={"dish_id": str(task_id), "agent": "codex"},
            )
        )
        assert owner_before.allowed_actions == ("inspect",)
        assert owner_before.data["legal_actions"] == ("inspect",)
        assert owner_before.data["agent_action"] == {
            "command": "inspect",
            "arguments": {
                "submission_id": started.data["operation_id"],
                "independence_attestation": verification.data[
                    "independence_attestation"
                ],
            },
        }

        foreign_before = port.execute(
            _call(
                "read",
                run_id=foreign_run,
                owner="foreign-owner",
                arguments={"dish_id": str(task_id), "agent": "claude"},
            )
        )
        assert foreign_before.allowed_actions == ()
        assert foreign_before.data["legal_actions"] == ()
        assert (
            foreign_before.data["verification_eligibility"]["rule"]
            == "VERIFICATION_OCCURRENCE_OWNED"
        )

        inspected = _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        owner_after = port.execute(
            _call(
                "read",
                run_id=verifier_run,
                owner="verifier-owner",
                arguments={"dish_id": str(task_id), "agent": "codex"},
            )
        )
        assert owner_after.allowed_actions == ("approve", "reject")
        assert owner_after.data["legal_actions"] == ("approve", "reject")
        assert owner_after.data["reviewed_identity"] == inspected.data["reviewed_identity"]
        assert (
            owner_after.data["independence_attestation"]
            == verification.data["independence_attestation"]
        )

        foreign_after = port.execute(
            _call(
                "read",
                run_id=foreign_run,
                owner="foreign-owner",
                arguments={"dish_id": str(task_id), "agent": "claude"},
            )
        )
        assert foreign_after.allowed_actions == ()
        assert foreign_after.data["legal_actions"] == ()

        bypass = port.execute(
            _call(
                "start",
                run_id=foreign_run,
                request_id=_next(ids),
                owner="foreign-owner",
                arguments={
                    "dish_id": str(task_id),
                    "kind": "verification",
                    "agent": "claude",
                    "independence_attestation": "independent",
                },
            )
        )
        assert bypass.ok is False
        assert session.scalar(
            select(func.count())
            .select_from(wf.OperationActorFact)
            .where(
                wf.OperationActorFact.operation_id
                == uuid.UUID(verification.data["operation_id"]),
                wf.OperationActorFact.actor_role == "verification",
            )
        ) == 1

        expired_at = _call(
            "read",
            run_id=verifier_run,
            owner="verifier-owner",
            arguments={"dish_id": str(task_id), "agent": "codex"},
        )
        same_expired = port.execute(
            replace(expired_at, now=expired_at.now + timedelta(minutes=11))
        )
        assert same_expired.allowed_actions == ()
        assert same_expired.data["legal_actions"] == ()
        assert same_expired.data["service_access"]["state"] == "same_run_recovery_required"

        foreign_expired_call = _call(
            "read",
            run_id=foreign_run,
            owner="foreign-owner",
            arguments={"dish_id": str(task_id), "agent": "claude"},
        )
        foreign_expired = port.execute(
            replace(
                foreign_expired_call,
                now=foreign_expired_call.now + timedelta(minutes=11),
            )
        )
        assert foreign_expired.allowed_actions == ("safe-reclaim",)
        assert foreign_expired.data["legal_actions"] == ("safe-reclaim",)
        reclaim_action = foreign_expired.data["agent_action"]
        assert reclaim_action == {
            "command": "safe-reclaim",
            "arguments": {
                "submission_id": started.data["operation_id"],
                "lease_id": verification.data["lease_id"],
                "agent": "claude",
            },
        }

        reclaim_call = _call(
            "safe-reclaim",
            run_id=foreign_run,
            request_id=_next(ids),
            owner="foreign-owner",
            arguments=reclaim_action["arguments"],
        )
        reclaimed = port.execute(
            replace(reclaim_call, now=reclaim_call.now + timedelta(minutes=11))
        )
        assert reclaimed.ok, (reclaimed.code, reclaimed.data)
        successor_action = reclaimed.data["agent_action"]
        successor_action["arguments"]["independence_attestation"] = "independent"
        successor_call = _call(
            successor_action["command"],
            run_id=foreign_run,
            request_id=_next(ids),
            owner="foreign-owner",
            arguments=successor_action["arguments"],
        )
        successor = port.execute(
            replace(successor_call, now=successor_call.now + timedelta(minutes=11))
        )
        assert successor.ok, (successor.code, successor.data)

        successor_read_call = _call(
            "read",
            run_id=foreign_run,
            owner="foreign-owner",
            arguments={"dish_id": str(task_id), "agent": "claude"},
        )
        successor_read = port.execute(
            replace(
                successor_read_call,
                now=successor_read_call.now + timedelta(minutes=11),
            )
        )
        assert successor_read.allowed_actions == ("inspect",)
        assert successor_read.data["agent_action"]["command"] == "inspect"
        inspect_arguments = dict(successor_read.data["agent_action"]["arguments"])
        inspect_arguments["agent"] = "claude"
        successor_inspect_call = _call(
            "inspect",
            run_id=foreign_run,
            request_id=_next(ids),
            owner="foreign-owner",
            principal="verification",
            arguments=inspect_arguments,
        )
        successor_inspected = port.execute(
            replace(
                successor_inspect_call,
                now=successor_inspect_call.now + timedelta(minutes=11),
            )
        )
        assert successor_inspected.ok, (
            successor_inspected.code,
            successor_inspected.data,
        )
