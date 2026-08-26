"""Native PostgreSQL scalar content/execution binding serialization."""
from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.repositories import ContractBindingRepository
from tests.support.postgresql.concurrency import independent_connections
from tests.support.postgresql.core import (
    NOW,
    _bootstrap_registry,
    _import_one,
    _next,
    core_db,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _wait_for_row_lock(
    observer_connection,
    *,
    backend_pid: int,
    future: Future[object],
    timeout: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if future.done():
            future.result()
            raise AssertionError("content transaction committed before execution-row serialization")
        wait_event_type = observer_connection.execute(
            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
            {"pid": backend_pid},
        ).scalar_one()
        if wait_event_type == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError("content transaction did not block on the execution-row lock")


def test_command_content_insert_serializes_with_concurrent_binding_update(
    request: pytest.FixtureRequest,
) -> None:
    factory, ids = request.getfixturevalue(core_db.__name__)
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        imported = _import_one(session, ids, context)
        state = session.get(
            models.DishState, (context["generation_id"], imported.task_id)
        )
        assert state is not None
        prior_content_id = state.current_content_version_id
        replacement_binding_id = _next(ids)
        ContractBindingRepository(session).add(
            models.HonestContractBinding(
                binding_id=replacement_binding_id,
                binding_kind="task_schema",
                source_identity="honest-pantry@native-binding-race",
                dish_release="dish-42619b9",
                honest_release="honest-1",
                protocol_release="protocol-1",
                protocol_sha256="d" * 64,
                schema_release="schema-native-binding-race",
                schema_sha256="e" * 64,
                migration_id=None,
                source_schema_version=None,
                target_schema_version=None,
                migration_metadata_sha256=None,
                source_ids={"fixture": "native-binding-race"},
                provenance={"fixture": True},
                resolved_at=NOW,
            )
        )
        run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
        session.add(
            wf.ServiceRun(
                run_id=run_id,
                generation_id=context["generation_id"],
                owner_id="native-binding-race",
                agent="service",
                capability_digest=b"b" * 32,
                bootstrap_id=None,
                status="active",
                registered_at=NOW,
                retired_at=None,
            )
        )
        session.flush()
        session.add(
            wf.ServiceRequest(
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner_id="native-binding-race",
                principal_class="service",
                command_name="native-binding-race",
                canonical_payload_sha256="f" * 64,
                canonical_payload={"fixture": "native-binding-race"},
                protocol_release="protocol-1",
                dish_release="dish-42619b9",
                admitted_at=NOW,
            )
        )
        session.flush()
        session.add(
            wf.CommandExecution(
                execution_id=execution_id,
                generation_id=context["generation_id"],
                request_id=request_id,
                task_id=imported.task_id,
                operation_id=None,
                command_name="native-binding-race",
                transaction_profile="L",
                canonical_intent={"fixture": "native-binding-race"},
                pinned_inputs={"now": NOW.isoformat()},
                contract_binding_id=context["binding_id"],
                status="pending",
                claim_owner=None,
                claim_token=None,
                claim_expires_at=None,
                execution_revision=1,
                admitted_at=NOW,
                terminal_at=None,
            )
        )
        content_id = _next(ids)

    engine = factory.kw["bind"]
    with independent_connections(engine, count=3) as (
        content_connection,
        binding_connection,
        observer_connection,
    ):
        content_transaction = content_connection.begin()
        binding_transaction = binding_connection.begin()
        content_connection.execute(
            insert(models.DishMutationReceipt),
            {
                "generation_id": context["generation_id"],
                "task_id": imported.task_id,
                "dish_version": 2,
                "source_route": "command_execution",
                "import_run_id": None,
                "command_execution_id": execution_id,
                "content_changed": True,
                "placement_changed": False,
                "completion_changed": False,
                "occurred_at": NOW + timedelta(seconds=1),
            },
        )
        content_connection.execute(
            insert(models.ContentVersion),
            {
                "content_version_id": content_id,
                "generation_id": context["generation_id"],
                "task_id": imported.task_id,
                "representation_kind": "document",
                "title": "Native binding race",
                "body": "Native binding race\n---\nStatus: ready\n",
                "identity_scheme": "legacy-sha256-v1",
                "content_identity": "f" * 64,
                "creator_route": "command_execution",
                "import_run_id": None,
                "command_execution_id": execution_id,
                "predecessor_content_version_id": prior_content_id,
                "contract_binding_id": context["binding_id"],
                "created_dish_version": 2,
                "created_at": NOW + timedelta(seconds=1),
            },
        )
        content_connection.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == context["generation_id"],
                models.DishState.task_id == imported.task_id,
                models.DishState.dish_version == 1,
            )
            .values(
                current_content_version_id=content_id,
                dish_version=2,
                updated_at=NOW + timedelta(seconds=1),
            )
        )
        binding_connection.execute(
            update(wf.CommandExecution)
            .where(wf.CommandExecution.execution_id == execution_id)
            .values(contract_binding_id=replacement_binding_id)
        )
        backend_pid = content_connection.execute(
            text("SELECT pg_backend_pid()")
        ).scalar_one()

        with ThreadPoolExecutor(max_workers=1) as pool:
            content_commit = pool.submit(content_transaction.commit)
            _wait_for_row_lock(
                observer_connection,
                backend_pid=backend_pid,
                future=content_commit,
            )
            binding_transaction.commit()
            with pytest.raises(DBAPIError, match="content creation receipt mismatch"):
                content_commit.result(timeout=20)

    with session_scope(factory) as session:
        state = session.get(
            models.DishState, (context["generation_id"], imported.task_id)
        )
        execution = session.get(wf.CommandExecution, execution_id)
        assert state is not None and state.dish_version == 1
        assert execution is not None
        assert execution.contract_binding_id == replacement_binding_id
        assert session.scalar(
            select(models.ContentVersion.content_version_id).where(
                models.ContentVersion.content_version_id == content_id
            )
        ) is None
