from __future__ import annotations

from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
)
from tests.support.postgresql.workflow import _next, _register_run


def test_ineligible_verification_caller_gets_handoff_and_replay(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    same_agent_run = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=same_agent_run,
            owner="other-owner",
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
        same_agent = port.execute(
            _call(
                "start",
                run_id=same_agent_run,
                request_id=_next(ids),
                owner="other-owner",
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "claude",
                    "independence_attestation": "independent",
                },
            )
        )

        for rejected in (same_run, same_agent):
            assert rejected.code == "VERIFIER_NOT_INDEPENDENT"
            assert rejected.allowed_actions == ()
            assert rejected.data["verification_eligibility"] == {
                "eligible": False,
                "rule": "VERIFIER_NOT_INDEPENDENT",
                "conflicting_actor_fact_id": rejected.data["conflicting_actor_fact_id"],
            }
            assert rejected.data["verification_handoff"] == {
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
            agent="codex",
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
                arguments={"dish_id": str(task_id), "agent": "codex"},
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
            agent="codex",
        )
        assert verification.ok
