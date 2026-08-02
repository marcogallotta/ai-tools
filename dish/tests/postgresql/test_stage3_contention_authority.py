from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.workflow import ContentionLost, WorkflowAuthorityService
from tests.support.postgresql.core import _import_one
from tests.support.postgresql.workflow import (
    NOW, _admit, _execution, _next, _register_run, workflow_db,
)


def test_ten_way_same_task_actor_lease_has_one_winner(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    contenders: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]] = []
    with session_scope(factory) as session:
        for index in range(10):
            run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
            owner = f"owner-{index}"
            _register_run(
                session,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner,
            )
            service = WorkflowAuthorityService(session)
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner,
            )
            _execution(
                service,
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                binding_id=context["binding_id"],
            )
            contenders.append((run_id, request_id, execution_id, _next(ids), owner))
        owner_execution = contenders[0][2]
        service = WorkflowAuthorityService(session)
        service.repo.capture_task_fence(
            execution_id=owner_execution,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        operation = service.create_operation(
            operation_id=_next(ids),
            execution_id=owner_execution,
            task_id=task_id,
            kind="initial",
            phase="prepare_required",
            persisted_actions=["prepare"],
            created_at=NOW,
        )
        operation_id = operation.operation_id

    barrier = threading.Barrier(10)

    def contend(index: int) -> str:
        run_id, _request_id, execution_id, lease_id, owner = contenders[index]
        barrier.wait()
        for attempt in range(5):
            try:
                with session_scope(factory) as session:
                    service = WorkflowAuthorityService(session)
                    service.acquire_actor_lease(
                        lease_id=lease_id,
                        execution_id=execution_id,
                        operation_id=operation_id,
                        run_id=run_id,
                        owner_id=owner,
                        actor_role="constructor",
                        actor_attempt_sequence=index + 1,
                        issued_at=NOW,
                        expires_at=NOW + timedelta(minutes=10),
                    )
                return "won"
            except (ContentionLost, IntegrityError):
                return "lost"
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        return "locked"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(contend, range(10)))
    assert results.count("won") == 1
    assert results.count("lost") == 9
    with session_scope(factory) as session:
        active = session.scalars(
            select(wf.ServiceLease).where(
                wf.ServiceLease.task_id == task_id,
                wf.ServiceLease.state == "active",
            )
        ).all()
        assert len(active) == 1


def test_independent_tasks_do_not_share_a_global_lease_fence(workflow_db) -> None:
    factory, ids, context, first_task_id = workflow_db
    with session_scope(factory) as session:
        second = _import_one(
            session,
            ids,
            context,
            task_id=_next(ids),
            asana_gid="987654321",
        )
        for index, task_id in enumerate((first_task_id, second.task_id), start=1):
            run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
            owner = f"independent-{index}"
            _register_run(session, generation_id=context["generation_id"], run_id=run_id, owner=owner)
            service = WorkflowAuthorityService(session)
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner,
            )
            _execution(
                service,
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                binding_id=context["binding_id"],
            )
            service.repo.capture_task_fence(
                execution_id=execution_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                at=NOW,
            )
            operation = service.create_operation(
                operation_id=_next(ids),
                execution_id=execution_id,
                task_id=task_id,
                kind="initial",
                phase="prepare_required",
                persisted_actions=["prepare"],
                created_at=NOW,
            )
            service.acquire_actor_lease(
                lease_id=_next(ids),
                execution_id=execution_id,
                operation_id=operation.operation_id,
                run_id=run_id,
                owner_id=owner,
                actor_role="constructor",
                actor_attempt_sequence=index,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceLease).where(wf.ServiceLease.state == "active")
        ) == 2


def test_ten_way_marco_authorization_reservation_is_single_winner(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    contenders: list[tuple[uuid.UUID, uuid.UUID]] = []
    with session_scope(factory) as session:
        admin_run = _next(ids)
        _register_run(
            session, generation_id=context["generation_id"], run_id=admin_run, owner="marco", agent="marco"
        )
        grant_request, grant_execution = _next(ids), _next(ids)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=grant_request,
            generation_id=context["generation_id"],
            run_id=admin_run,
            command="authorize-governed-change",
            owner="marco",
            principal="admin",
        )
        _execution(
            service,
            execution_id=grant_execution,
            request_id=grant_request,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
            command="authorize-governed-change",
        )
        grant_id = _next(ids)
        service.grant_marco_authorization(
            grant_id=grant_id,
            execution_id=grant_execution,
            task_id=task_id,
            operation_id=None,
            field_name="title",
            before_value="old",
            after_value="new",
            reason="approved exact governed change",
            actor="Marco",
            run_id=admin_run,
            granted_at=NOW,
        )
        for index in range(10):
            request_id, execution_id = _next(ids), _next(ids)
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=admin_run,
                command="prepare",
                owner="marco",
                principal="admin",
                payload={"candidate": index},
            )
            _execution(
                service,
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                binding_id=context["binding_id"],
                command="prepare",
            )
            contenders.append((execution_id, _next(ids)))
    barrier = threading.Barrier(10)
    def reserve(item: tuple[uuid.UUID, uuid.UUID]) -> str:
        execution_id, token = item
        barrier.wait()
        for attempt in range(5):
            try:
                with session_scope(factory) as session:
                    WorkflowAuthorityService(session).reserve_marco_authorization(
                        grant_id=grant_id,
                        reservation_token=token,
                        execution_id=execution_id,
                        reserved_at=NOW,
                    )
                return "won"
            except ContentionLost:
                return "lost"
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        return "locked"
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(reserve, contenders))
    assert results.count("won") == 1
    assert results.count("lost") == 9
    with session_scope(factory) as session:
        state = session.get(wf.MarcoAuthorizationState, grant_id)
        assert state is not None and state.state == "reserved"
        assert session.scalar(
            select(func.count()).select_from(wf.MarcoAuthorizationEvent).where(
                wf.MarcoAuthorizationEvent.grant_id == grant_id,
                wf.MarcoAuthorizationEvent.event_kind == "reserved",
            )
        ) == 1
