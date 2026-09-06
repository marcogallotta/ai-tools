from __future__ import annotations

from sqlalchemy import select

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from tests.postgresql.test_stage4_command_port import (
    _prepare_change,
    _signed_ready_baseline,
    _start_change,
    _start_planning_operation,
)
from tests.support.postgresql.command import (
    _add_destination_section,
    _add_verification_queue,
    _call,
    _port,
    _prepare_for_verification,
    _start_initial,
)
from tests.support.postgresql.workflow import _next, _register_run


def test_verification_rejects_author_from_prior_operation_on_same_task(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, signed = _signed_ready_baseline(session, ids, context, task_id)
        prior_author = session.scalar(
            select(wf.OperationActorFact)
            .where(
                wf.OperationActorFact.task_id == task_id,
                wf.OperationActorFact.actor_role == "author",
            )
            .order_by(
                wf.OperationActorFact.recorded_at,
                wf.OperationActorFact.actor_attempt_sequence,
            )
            .limit(1)
        )
        assert prior_author is not None

        change_run, started = _start_change(port, ids, context, task_id)
        candidate = (signed.title + "\n" + signed.body).replace(
            "Crisp and aromatic.",
            "Crisp, aromatic, and deeply browned.",
        )
        prepared = _prepare_change(
            port,
            ids,
            task_id,
            change_run,
            started.data["operation_id"],
            candidate,
        )
        assert prepared.ok, (prepared.code, prepared.http_status, prepared.data)
        assert prepared.data["handoff"] == "verification"

        blocked = port.execute(
            _call(
                "start",
                run_id=prior_author.run_id,
                request_id=_next(ids),
                owner=prior_author.owner_id,
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": prior_author.agent,
                    "independence_attestation": "independent",
                },
            )
        )
        assert not blocked.ok
        assert blocked.code == "VERIFIER_NOT_INDEPENDENT"
        assert blocked.data["conflicting_actor_fact_id"] == str(prior_author.actor_fact_id)


def test_planning_only_run_can_verify_later_initial_candidate(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    planner_run = _next(ids)
    research_run = _next(ids)
    planning = """### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: main
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
"""
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _add_destination_section(session, ids, context, external_id="12345")
        _register_run(session, generation_id=context["generation_id"], run_id=planner_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=research_run,
            owner="research-owner",
            agent="codex",
        )
        port = _port(session, ids)
        planned = _start_planning_operation(port, ids, task_id=task_id, run_id=planner_run)
        planning_handoff = port.execute(
            _call(
                "prepare",
                run_id=planner_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": planned.data["operation_id"],
                    "file_text": planning,
                    "agent": "claude",
                    "model": "test-model",
                },
            )
        )
        assert planning_handoff.ok, (
            planning_handoff.code,
            planning_handoff.http_status,
            planning_handoff.data,
        )
        assert planning_handoff.data["handoff"] == "planning-to-research"

        started = _start_initial(
            port,
            ids,
            task_id=task_id,
            run_id=research_run,
            owner="research-owner",
            agent="codex",
        )
        _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=research_run,
            owner="research-owner",
            agent="codex",
        )

        verifier = port.execute(
            _call(
                "start",
                run_id=planner_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "claude",
                    "independence_attestation": "independent",
                },
            )
        )
        assert verifier.ok, (verifier.code, verifier.http_status, verifier.data)
