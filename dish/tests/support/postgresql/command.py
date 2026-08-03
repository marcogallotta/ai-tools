"""Command-port builders shared by PostgreSQL semantic tests."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from dish_pg import models
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.transition import ProjectionService
from tests.support.postgresql.workflow import NOW, _next, _register_run

SECRET = b"stage-4-cursor-secret-32-bytes!!"


def _port(session, ids) -> PostgresCommandPort:
    generation_id = session.scalar(
        select(models.AuthorityGeneration.generation_id).where(
            models.AuthorityGeneration.status == "active"
        )
    )
    ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
        generation_id=generation_id,
        activation_reason="Stage 4 command-port test authority",
        created_at=NOW,
        external_effects_enabled=True,
    )
    return PostgresCommandPort(
        session,
        cursor_secret=SECRET,
        uuid_factory=lambda: _next(ids),
        lease_duration=timedelta(minutes=10),
    )


def _call(command, *, run_id, request_id=None, arguments=None, principal="agent", owner="owner-1"):
    return CommandCall(
        command_name=command,
        arguments=arguments or {},
        owner_id=owner,
        principal_class=principal,
        run_id=run_id,
        request_id=request_id,
        now=NOW,
    )


def _add_verification_queue(session, ids, context) -> uuid.UUID:
    section_id = _next(ids)
    session.add(
        models.GovernedSection(
            section_id=section_id,
            project_id=context["project_id"],
            logical_name="Verification Queue",
            lifecycle="active",
            import_run_id=context["import_run_id"],
            created_at=NOW,
            retired_at=None,
        )
    )
    session.add(
        models.SectionExternalAlias(
            alias_id=_next(ids),
            section_id=section_id,
            external_system="asana",
            external_id="1217084805070732",
            origin="imported",
            import_run_id=context["import_run_id"],
            projection_event_id=None,
            state="active",
            created_at=NOW,
            retired_at=None,
        )
    )
    session.add(
        models.SectionRegistryEntry(
            registry_version_id=context["registry_version_id"],
            section_id=section_id,
            ordinal=1,
            display_name="Verification Queue",
            workflow_role="verification_queue",
        )
    )
    session.flush()
    return section_id


def _start_initial(port: PostgresCommandPort, ids, *, task_id, run_id, owner="owner-1", agent="claude"):
    result = port.execute(
        _call(
            "start",
            run_id=run_id,
            request_id=_next(ids),
            owner=owner,
            arguments={"task_id": str(task_id), "kind": "initial", "agent": agent},
        )
    )
    assert result.ok
    return result


def _prepare_for_verification(port: PostgresCommandPort, ids, *, task_id, operation_id, run_id, owner="owner-1"):
    result = port.execute(
        _call(
            "prepare",
            run_id=run_id,
            request_id=_next(ids),
            owner=owner,
            arguments={
                "task_id": str(task_id),
                "operation_id": operation_id,
                "file_text": "Candidate body\n---\nStatus: pending-verification\n",
            },
        )
    )
    assert result.ok
    return result


def _inspect(
    port: PostgresCommandPort,
    ids,
    *,
    task_id,
    operation_id,
    run_id,
    owner="verifier-owner",
    agent="codex",
):
    result = port.execute(
        _call(
            "inspect",
            run_id=run_id,
            request_id=_next(ids),
            owner=owner,
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": operation_id,
                "agent": agent,
                "independence_attestation": "I independently inspected this exact candidate.",
            },
        )
    )
    assert result.ok
    return result


def _verification_ready(session, ids, context, task_id):
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
    prepared = _prepare_for_verification(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=author_run,
    )
    inspected = _inspect(
        port,
        ids,
        task_id=task_id,
        operation_id=started.data["operation_id"],
        run_id=verifier_run,
    )
    return port, author_run, verifier_run, started, prepared, inspected
