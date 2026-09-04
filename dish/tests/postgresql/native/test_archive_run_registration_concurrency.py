"""Native PostgreSQL archive/run-registration linearization regressions."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.workflow import WorkflowAuthorityRepository, WorkflowAuthorityService
from tests.support.postgresql.concurrency import (
    TransactionGate,
    assert_transaction_blocked,
    independent_connections,
    managed_session,
)
from tests.support.postgresql.core import core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]
SECRET = b"native-archive-registration-secret"


@pytest.fixture(autouse=True)
def _require_native_postgresql(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--postgresql"):
        pytest.skip("native PostgreSQL archive concurrency certification requires --postgresql")


class _ObserveArchiveLockRepository(WorkflowAuthorityRepository):
    def __init__(self, *args, before: Event, after: Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._before = before
        self._after = after

    def lock_service_runs_for_archive(self) -> None:
        self._before.set()
        super().lock_service_runs_for_archive()
        self._after.set()


class _PauseAfterArchiveLockRepository(WorkflowAuthorityRepository):
    def __init__(self, *args, gate: TransactionGate, **kwargs):
        super().__init__(*args, **kwargs)
        self._gate = gate

    def lock_service_runs_for_archive(self) -> None:
        super().lock_service_runs_for_archive()
        self._gate.block()


class _SignalingRegistrationRepository(WorkflowAuthorityRepository):
    def __init__(self, *args, entered: Event, returned: Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._entered = entered
        self._returned = returned

    def register_run(self, row: wf.ServiceRun) -> None:
        self._entered.set()
        super().register_run(row)
        self._returned.set()


def _archive_call(*, run_id, task_id) -> CommandCall:
    return CommandCall(
        command_name="archive",
        arguments={"task_id": str(task_id), "confirmed": True},
        owner_id="Marco",
        principal_class="admin",
        run_id=run_id,
        request_id=uuid.uuid4(),
        now=NOW,
    )


def _start_call(*, run_id, task_id) -> CommandCall:
    return CommandCall(
        command_name="start",
        arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
        owner_id="owner-1",
        principal_class="agent",
        run_id=run_id,
        request_id=uuid.uuid4(),
        now=NOW,
    )


def _port(session) -> PostgresCommandPort:
    return PostgresCommandPort(session, cursor_secret=SECRET, uuid_factory=uuid.uuid4)


def _register_with_timestamp(session, *, generation_id, run_id, registered_at) -> None:
    WorkflowAuthorityService(session).register_run(
        run_id=run_id,
        generation_id=generation_id,
        owner_id="owner-1",
        agent="claude",
        capability_digest=run_id.bytes + run_id.bytes,
        registered_at=registered_at,
    )


def test_registration_wins_and_is_inside_archive_tombstone_boundary(core_db) -> None:
    factory, _ids, context, task_id = native_workflow_db(core_db)
    admin_run = uuid.uuid4()
    registration_run = uuid.uuid4()
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
    engine = factory.kw["bind"]
    before_lock = Event()
    after_lock = Event()

    with independent_connections(engine) as (registration_connection, archive_connection):
        registration_session = Session(bind=registration_connection, expire_on_commit=False)
        try:
            # Deliberately use a future timestamp: commit/lock order, not the
            # caller timestamp, defines the pre/post archive boundary.
            _register_with_timestamp(
                registration_session,
                generation_id=context["generation_id"],
                run_id=registration_run,
                registered_at=NOW + timedelta(days=30),
            )

            def archive_worker():
                with managed_session(archive_connection) as session:
                    port = _port(session)
                    port.workflow.repo = _ObserveArchiveLockRepository(
                        session, before=before_lock, after=after_lock
                    )
                    return port.execute(_archive_call(run_id=admin_run, task_id=task_id))

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(archive_worker)
                assert before_lock.wait(timeout=30)
                assert after_lock.wait(timeout=0.2) is False
                assert_transaction_blocked(future)
                registration_session.commit()
                archived = future.result(timeout=30)
        finally:
            registration_session.close()

    assert archived.ok, archived
    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(wf.TaskRunRevocation).where(
                wf.TaskRunRevocation.generation_id == context["generation_id"],
                wf.TaskRunRevocation.task_id == task_id,
                wf.TaskRunRevocation.run_id == registration_run,
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(models.DishMutationReceipt).where(
                models.DishMutationReceipt.generation_id == context["generation_id"],
                models.DishMutationReceipt.task_id == task_id,
                models.DishMutationReceipt.archive_changed.is_(True),
            )
        ) == 1
        state = session.get(models.DishState, (context["generation_id"], task_id))
        state.archived_at = None
        session.flush()
        stale = _port(session).execute(_start_call(run_id=registration_run, task_id=task_id))
        assert stale.ok is False
        assert stale.code == "AUTHORITY_MISMATCH"


def test_archive_wins_and_late_registration_is_fresh_after_boundary(core_db) -> None:
    factory, _ids, context, task_id = native_workflow_db(core_db)
    admin_run = uuid.uuid4()
    late_run = uuid.uuid4()
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
    engine = factory.kw["bind"]
    gate = TransactionGate(label="archive holds service_runs SHARE lock")
    registration_entered = Event()
    registration_returned = Event()

    def archive_worker(connection):
        with managed_session(connection) as session:
            port = _port(session)
            port.workflow.repo = _PauseAfterArchiveLockRepository(session, gate=gate)
            return port.execute(_archive_call(run_id=admin_run, task_id=task_id))

    def registration_worker(connection):
        with managed_session(connection) as session:
            service = WorkflowAuthorityService(session)
            service.repo = _SignalingRegistrationRepository(
                session, entered=registration_entered, returned=registration_returned
            )
            service.register_run(
                run_id=late_run,
                generation_id=context["generation_id"],
                owner_id="owner-1",
                agent="claude",
                capability_digest=late_run.bytes + late_run.bytes,
                # Deliberately old: timestamp cannot move this post-lock winner
                # to the pre-archive side.
                registered_at=NOW - timedelta(days=30),
            )

    with independent_connections(engine) as (archive_connection, registration_connection):
        with ThreadPoolExecutor(max_workers=2) as pool:
            archive_future = pool.submit(archive_worker, archive_connection)
            gate.wait_until_blocked()
            registration_future = pool.submit(registration_worker, registration_connection)
            assert registration_entered.wait(timeout=30)
            assert registration_returned.wait(timeout=0.2) is False
            assert_transaction_blocked(registration_future)
            gate.release()
            archived = archive_future.result(timeout=30)
            registration_future.result(timeout=30)

    assert archived.ok, archived
    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(wf.TaskRunRevocation).where(
                wf.TaskRunRevocation.generation_id == context["generation_id"],
                wf.TaskRunRevocation.task_id == task_id,
                wf.TaskRunRevocation.run_id == admin_run,
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(wf.TaskRunRevocation).where(
                wf.TaskRunRevocation.generation_id == context["generation_id"],
                wf.TaskRunRevocation.task_id == task_id,
                wf.TaskRunRevocation.run_id == late_run,
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(models.DishMutationReceipt).where(
                models.DishMutationReceipt.generation_id == context["generation_id"],
                models.DishMutationReceipt.task_id == task_id,
                models.DishMutationReceipt.archive_changed.is_(True),
            )
        ) == 1
        state = session.get(models.DishState, (context["generation_id"], task_id))
        state.archived_at = None
        session.flush()
        fresh = _port(session).execute(_start_call(run_id=late_run, task_id=task_id))
        assert fresh.ok, fresh
