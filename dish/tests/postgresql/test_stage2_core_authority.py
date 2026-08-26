from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.repositories import (
    AuthorityRepository,
    ContractBindingRepository,
    CoreAuthorityError,
    DishRepository,
    RegistryRepository,
    ScalarMutationSource,
)
from dish_pg.workflow import OperationRunRevoked, WorkflowAuthorityRepository
from dish_pg.services import (
    CoreAuthorityService,
    ImportedOperationHistorySpec,
    ImportedOperationRunRevocationSpec,
    ImportedServiceLeaseSpec,
    ImportedTaskSpec,
    ImportedWorkflowOperationSpec,
)
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _next, core_db

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 1, 19, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def test_stage2_schema_stops_before_command_authority() -> None:
    assert set(models.CORE_TABLE_NAMES) == {
        "stage_a_import_runs",
        "authority_generations",
        "generation_bootstrap_authorities",
        "authority_activations",
        "applied_migration_events",
        "honest_contract_bindings",
        "governed_projects",
        "governed_sections",
        "section_registry_versions",
        "section_registry_entries",
        "section_registry_activations",
        "active_section_registries",
        "project_external_aliases",
        "section_external_aliases",
        "dish_tasks",
        "task_external_aliases",
        "task_content_versions",
        "dish_mutation_receipts",
        "dish_states",
        "task_membership_heads",
        "task_project_membership_events",
        "current_task_project_memberships",
    }
    forbidden = {
        "service_requests",
        "command_executions",
        "operations",
        "verification_cycles",
        "service_leases",
        "projection_outbox",
    }
    assert forbidden.isdisjoint(models.CORE_TABLE_NAMES)


def test_stage2_migration_renders_postgresql_constraints_and_guards() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, "head", sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE dish_tasks" in rendered
    assert "CREATE TABLE task_content_versions" in rendered
    assert "uq_authority_generations_one_active" in rendered
    assert "uq_project_external_alias_identity" in rendered
    assert "uq_section_external_alias_identity" in rendered
    assert "uq_task_external_alias_identity" in rendered
    assert "dish_reject_immutable_authority" in rendered
    assert "dish_validate_active_registry_pointer" in rendered
    assert "dish_validate_scalar_state" in rendered
    assert "task_external_aliases_identity_update" in rendered


def test_stage2_alembic_upgrade_reaches_head_from_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "stage2.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "0042_scalar_dish_state")

    engine = create_engine(database_url, future=True)
    try:
        table_names = set(inspect(engine).get_table_names())
        assert set(models.CORE_TABLE_NAMES).issubset(table_names)
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "0042_scalar_dish_state"
    finally:
        engine.dispose()


def test_external_alias_constraints_are_table_local_and_uniquely_named() -> None:
    unique_names: set[str] = set()
    for table_name in (
        "project_external_aliases",
        "section_external_aliases",
        "task_external_aliases",
    ):
        table = models.Base.metadata.tables[table_name]
        assert all(constraint.table is table for constraint in table.constraints)
        identity = next(
            constraint
            for constraint in table.constraints
            if constraint.name and constraint.name.endswith("external_alias_identity")
        )
        assert identity.name not in unique_names
        unique_names.add(identity.name)


def test_import_activation_commits_complete_authority_bundle(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        result = _import_one(session, ids, context)

    with factory() as session:
        task = session.get(models.DishTask, result.task_id)
        assert task is not None
        assert task.creation_route == "import"
        state = session.get(
            models.DishState,
            (context["generation_id"], result.task_id),
        )
        assert state is not None
        assert state.current_content_version_id == result.content_version_id
        assert state.dish_version == 1
        version = session.get(models.ContentVersion, result.content_version_id)
        assert version is not None
        assert version.title == "[ready] Exact imported task"
        assert version.body.endswith("Status: ready\n")
        assert state.section_id == context["section_id"]
        assert state.registry_version_id == context["registry_version_id"]
        assert state.completed is False
        memberships = session.scalars(
            select(models.CurrentTaskProjectMembership).where(
                models.CurrentTaskProjectMembership.task_id == result.task_id
            )
        ).all()
        assert [(row.project_id, row.is_member) for row in memberships] == [
            (context["project_id"], True)
        ]
        alias = session.scalar(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.external_id == "123456789"
            )
        )
        assert alias is not None and alias.task_id == result.task_id


def test_scalar_noop_writes_no_receipt(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        imported = _import_one(session, ids, context)
        state = session.get(
            models.DishState, (context["generation_id"], imported.task_id)
        )
        head = session.get(
            models.TaskMembershipHead,
            (context["generation_id"], imported.task_id),
        )
        assert state is not None and head is not None
        mutation = DishRepository(
            session, uuid_factory=lambda: _next(ids)
        ).begin_scalar_mutation(
            generation_id=context["generation_id"],
            task_id=imported.task_id,
            expected_dish_version=state.dish_version,
            expected_membership_revision=head.membership_revision,
            source=ScalarMutationSource(
                route="import",
                import_run_id=context["import_run_id"],
                occurred_at=NOW + timedelta(seconds=1),
            ),
        )
        outcome = mutation.finalize()
        assert outcome.dish_version is None
        assert outcome.changed_domains == frozenset()
        assert session.scalar(
            select(func.count()).select_from(models.DishMutationReceipt)
        ) == 1


def test_multi_domain_scalar_mutation_writes_one_receipt_and_one_version(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        imported = _import_one(session, ids, context)
        state = session.get(
            models.DishState, (context["generation_id"], imported.task_id)
        )
        head = session.get(
            models.TaskMembershipHead,
            (context["generation_id"], imported.task_id),
        )
        assert state is not None and head is not None
        mutation = DishRepository(
            session, uuid_factory=lambda: _next(ids)
        ).begin_scalar_mutation(
            generation_id=context["generation_id"],
            task_id=imported.task_id,
            expected_dish_version=state.dish_version,
            expected_membership_revision=head.membership_revision,
            source=ScalarMutationSource(
                route="import",
                import_run_id=context["import_run_id"],
                occurred_at=NOW + timedelta(seconds=1),
            ),
        )
        replacement_id = mutation.replace_content(
            title="[ready] Replacement",
            body="Replacement body\n---\nStatus: ready\n",
            identity_scheme="legacy-sha256-v1",
            content_identity=HASH_C,
            contract_binding_id=context["binding_id"],
            predecessor_content_version_id=state.current_content_version_id,
        )
        mutation.place(
            section_id=state.section_id,
            registry_version_id=state.registry_version_id,
        )
        mutation.set_completion(completed=True, reason="imported")
        outcome = mutation.finalize()

        assert outcome.dish_version == 2
        assert outcome.changed_domains == frozenset(
            {"content", "placement", "completion"}
        )
        receipt = session.get(
            models.DishMutationReceipt,
            (context["generation_id"], imported.task_id, 2),
        )
        assert receipt is not None
        assert receipt.content_changed is True
        assert receipt.placement_changed is True
        assert receipt.completion_changed is True
        session.refresh(state)
        assert state.current_content_version_id == replacement_id
        assert state.dish_version == state.placement_version == state.completion_version == 2
        assert session.scalar(
            select(func.count()).select_from(models.DishMutationReceipt)
        ) == 2


def test_import_backfills_terminal_operation_attempt_history_without_fake_authority(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        task_id, operation_id, lease_id, revocation_id, source_run_id = (
            _next(ids), _next(ids), _next(ids), _next(ids), _next(ids)
        )
        legacy_run_text = str(source_run_id).upper()
        CoreAuthorityService(session, uuid_factory=lambda: _next(ids)).import_task_document(
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            contract_binding_id=context["binding_id"],
            spec=ImportedTaskSpec(
                task_id=task_id,
                asana_task_gid="123456788",
                title="[ready] Imported history",
                body="Canonical body\n---\nStatus: ready\n",
                identity_scheme="legacy-sha256-v1",
                content_identity=HASH_A,
                project_ids=(context["project_id"],),
                section_id=context["section_id"],
                completed=False,
                observed_at=NOW,
                operation_history=ImportedOperationHistorySpec(
                    operations=(ImportedWorkflowOperationSpec(
                        operation_id=operation_id, kind="planning", status="completed",
                        phase="terminal", terminal_outcome="planning_handoff_confirmed",
                        created_at=NOW, completed_at=NOW,
                    ),),
                    leases=(ImportedServiceLeaseSpec(
                        lease_id=lease_id, operation_id=operation_id, source_run_id=legacy_run_text,
                        owner_id="legacy-owner", lease_kind="actor", actor_attempt_sequence=1,
                        verification_cycle_id=None, issued_at=NOW,
                        expires_at=NOW + timedelta(minutes=1), released_at=NOW,
                    ),),
                    revocations=(ImportedOperationRunRevocationSpec(
                        revocation_id=revocation_id,
                        operation_id=operation_id,
                        owner_id="legacy-owner",
                        source_run_id=legacy_run_text,
                        source_lease_id=lease_id,
                        reason="operator killed exact legacy run",
                        revoked_at=NOW,
                    ),),
                ),
            ),
        )

    with factory() as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        lease = session.get(wf.ServiceLease, lease_id)
        assert operation is not None and operation.import_run_id == context["import_run_id"]
        assert operation.creation_request_id is None and operation.creation_execution_id is None
        assert lease is not None and lease.import_run_id == context["import_run_id"]
        assert lease.run_id is None and lease.source_run_id == legacy_run_text
        revocation = session.get(wf.OperationRunRevocation, revocation_id)
        assert revocation is not None and revocation.import_run_id == context["import_run_id"]
        assert revocation.run_id is None and revocation.source_run_id == legacy_run_text
        with pytest.raises(OperationRunRevoked):
            WorkflowAuthorityRepository(session).assert_operation_run_not_revoked(
                generation_id=context["generation_id"],
                operation_id=operation_id,
                owner_id="legacy-owner",
                run_id=source_run_id,
            )
        assert PostgresCommandPort(session, cursor_secret=b"t" * 32)._next_actor_attempt_sequence(task_id) == 2


def test_import_rejects_uncertain_operation_even_with_terminal_evidence(core_db) -> None:
    factory, ids = core_db
    with pytest.raises(CoreAuthorityError, match="terminal completed/cancelled"):
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids)
            CoreAuthorityService(session, uuid_factory=lambda: _next(ids)).import_task_document(
                generation_id=context["generation_id"],
                import_run_id=context["import_run_id"],
                contract_binding_id=context["binding_id"],
                spec=ImportedTaskSpec(
                    task_id=_next(ids), asana_task_gid="123456787", title="[ready] Invalid history",
                    body="Canonical body\n---\nStatus: ready\n", identity_scheme="legacy-sha256-v1",
                    content_identity=HASH_A, project_ids=(context["project_id"],),
                    section_id=context["section_id"], completed=False, observed_at=NOW,
                    operation_history=ImportedOperationHistorySpec(operations=(
                        ImportedWorkflowOperationSpec(
                            operation_id=_next(ids), kind="planning", status="uncertain",
                            phase="terminal", terminal_outcome="planning_handoff_confirmed",
                            created_at=NOW, completed_at=NOW,
                        ),
                    )),
                ),
            )


def test_import_failure_rolls_back_the_whole_bundle(core_db) -> None:
    factory, ids = core_db
    task_id = _next(ids)
    with pytest.raises(CoreAuthorityError, match="active registry"):
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids)
            unknown_section_id = _next(ids)
            _import_one(
                session,
                ids,
                context,
                task_id=task_id,
                section_id=unknown_section_id,
            )

    with factory() as session:
        assert session.get(models.DishTask, task_id) is None
        assert session.scalar(select(func.count()).select_from(models.DishTask)) == 0
        assert session.scalar(select(func.count()).select_from(models.ContentVersion)) == 0


def test_external_alias_cannot_transfer_between_tasks(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        _import_one(session, ids, context, asana_gid="123456789")

    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            _import_one(session, ids, context, asana_gid="123456789")

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(models.DishTask)) == 1
        assert session.scalar(select(func.count()).select_from(models.TaskExternalAlias)) == 1


def test_content_and_occurrence_evidence_is_immutable(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        result = _import_one(session, ids, context)

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            version = session.get(models.ContentVersion, result.content_version_id)
            assert version is not None
            version.body = "mutated"
            session.flush()

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            receipt = session.get(
                models.DishMutationReceipt,
                (context["generation_id"], result.task_id, result.completion_version),
            )
            assert receipt is not None
            session.delete(receipt)
            session.flush()


def test_dish_creation_provenance_and_scalar_heads_are_not_rekeyable(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        imported = _import_one(session, ids, context)

    with pytest.raises(IntegrityError, match="creation provenance is immutable"):
        with session_scope(factory) as session:
            task = session.get(models.DishTask, imported.task_id)
            assert task is not None
            task.created_at = task.created_at + timedelta(seconds=1)
            session.flush()

    with pytest.raises(IntegrityError, match="dish_states cannot be deleted"):
        with session_scope(factory) as session:
            state = session.get(
                models.DishState,
                (context["generation_id"], imported.task_id),
            )
            assert state is not None
            session.delete(state)
            session.flush()

    with pytest.raises(IntegrityError, match="task_membership_heads identity is immutable"):
        with session_scope(factory) as session:
            head = session.get(
                models.TaskMembershipHead,
                (context["generation_id"], imported.task_id),
            )
            assert head is not None
            head.task_id = _next(ids)
            session.flush()
