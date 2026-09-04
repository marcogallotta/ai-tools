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
        "sections",
        "section_catalog_versions",
        "section_catalog_entries",
        "section_catalog_activations",
        "active_section_catalogs",
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
    assert "task_content_versions_scalar_source_validate" in rendered
    assert "command_executions_content_binding_guard" in rendered
    assert "FOR SHARE" in rendered
    assert "CREATE TRIGGER current_task_project_memberships_validate" in rendered
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
        native_catalog_tables = {
            "sections",
            "section_catalog_versions",
            "section_catalog_entries",
            "section_catalog_activations",
            "active_section_catalogs",
        }
        assert (set(models.CORE_TABLE_NAMES) - native_catalog_tables).issubset(
            table_names
        )
        assert native_catalog_tables.isdisjoint(table_names)
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='trigger' "
                        "AND (name LIKE 'dish_states_%' "
                        "OR name LIKE 'dish_mutation_receipts_%' "
                        "OR name LIKE 'task_membership_heads_%' "
                        "OR name LIKE 'task_content_versions_%' "
                        "OR name LIKE 'verification_inspection_%' "
                        "OR name='dish_tasks_creation_provenance_immutable' "
                        "OR name='command_executions_content_binding_guard' "
                        "OR name='projection_outbox_events_authority_insert' "
                        "OR name LIKE 'current_task_project_memberships_validate_%')"
                    )
                )
            }
        assert revision == "0042_scalar_dish_state"
        assert {
            "dish_states_validate_insert",
            "dish_states_validate_update",
            "dish_states_identity_immutable",
            "dish_states_delete_forbidden",
            "dish_mutation_receipts_immutable_update",
            "dish_mutation_receipts_immutable_delete",
            "task_membership_heads_identity_immutable",
            "task_membership_heads_delete_forbidden",
            "task_content_versions_scalar_source_validate",
            "task_content_versions_immutable_update",
            "task_content_versions_immutable_delete",
            "verification_inspection_placement_validate",
            "dish_tasks_creation_provenance_immutable",
            "command_executions_content_binding_guard",
            "projection_outbox_events_authority_insert",
            "current_task_project_memberships_validate_insert",
            "current_task_project_memberships_validate_update",
        }.issubset(triggers)
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


def test_ordinary_initial_scalar_authority_must_use_v1_all_domain_receipt(core_db) -> None:
    factory, ids = core_db
    with pytest.raises(IntegrityError, match="invalid initial DishState authority"):
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids)
            task_id = _next(ids)
            content_id = _next(ids)
            session.add(
                models.DishTask(
                    task_id=task_id,
                    existence_state="ordinary",
                    creation_route="import",
                    import_run_id=context["import_run_id"],
                    command_execution_id=None,
                    created_at=NOW,
                    retired_at=None,
                )
            )
            session.flush()
            session.add(
                models.DishMutationReceipt(
                    generation_id=context["generation_id"],
                    task_id=task_id,
                    dish_version=2,
                    source_route="import",
                    import_run_id=context["import_run_id"],
                    command_execution_id=None,
                    content_changed=True,
                    placement_changed=True,
                    completion_changed=True,
                    occurred_at=NOW,
                )
            )
            session.flush()
            session.add(
                models.ContentVersion(
                    content_version_id=content_id,
                    generation_id=context["generation_id"],
                    task_id=task_id,
                    representation_kind="document",
                    title="Invalid non-v1 initial content",
                    body="Invalid\n---\nStatus: ready\n",
                    identity_scheme="legacy-sha256-v1",
                    content_identity="d" * 64,
                    creator_route="import",
                    import_run_id=context["import_run_id"],
                    command_execution_id=None,
                    predecessor_content_version_id=None,
                    contract_binding_id=context["binding_id"],
                    created_dish_version=2,
                    created_at=NOW,
                )
            )
            session.flush()
            session.add(
                models.DishState(
                    generation_id=context["generation_id"],
                    task_id=task_id,
                    current_content_version_id=content_id,
                    section_id=context["section_id"],
                    registry_version_id=context["registry_version_id"],
                    completed=False,
                    completion_reason="imported",
                    dish_version=2,
                    placement_version=2,
                    completion_version=2,
                    updated_at=NOW,
                )
            )
            session.flush()


def test_initial_v1_content_source_must_match_dish_creation_source(core_db) -> None:
    factory, ids = core_db
    with pytest.raises(IntegrityError, match="content creation receipt mismatch"):
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids)
            other_import_run_id = _next(ids)
            session.add(
                models.ImportRun(
                    import_run_id=other_import_run_id,
                    source_commit="42619b9",
                    source_release="dish-42619b9",
                    legacy_generation_id="legacy-mismatched-source",
                    baseline_high_water_mark="asana-event-mismatched-source",
                    source_bundle_sha256="d" * 64,
                    status="complete",
                    started_at=NOW,
                    completed_at=NOW,
                    provenance={"fixture": "mismatched-source"},
                )
            )
            task_id = _next(ids)
            session.add(
                models.DishTask(
                    task_id=task_id,
                    existence_state="ordinary",
                    creation_route="import",
                    import_run_id=context["import_run_id"],
                    command_execution_id=None,
                    created_at=NOW,
                    retired_at=None,
                )
            )
            session.flush()
            session.add(
                models.DishMutationReceipt(
                    generation_id=context["generation_id"],
                    task_id=task_id,
                    dish_version=1,
                    source_route="import",
                    import_run_id=other_import_run_id,
                    command_execution_id=None,
                    content_changed=True,
                    placement_changed=True,
                    completion_changed=True,
                    occurred_at=NOW,
                )
            )
            session.flush()
            session.add(
                models.ContentVersion(
                    content_version_id=_next(ids),
                    generation_id=context["generation_id"],
                    task_id=task_id,
                    representation_kind="document",
                    title="Mismatched initial source",
                    body="Mismatched initial source\n---\nStatus: ready\n",
                    identity_scheme="legacy-sha256-v1",
                    content_identity="e" * 64,
                    creator_route="import",
                    import_run_id=other_import_run_id,
                    command_execution_id=None,
                    predecessor_content_version_id=None,
                    contract_binding_id=context["binding_id"],
                    created_dish_version=1,
                    created_at=NOW,
                )
            )
            session.flush()


def test_current_membership_pointer_must_match_exact_event(core_db) -> None:
    factory, ids = core_db
    with pytest.raises(IntegrityError, match="current project membership pointer is invalid"):
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids)
            imported = _import_one(session, ids, context)
            membership = session.scalar(
                select(models.CurrentTaskProjectMembership).where(
                    models.CurrentTaskProjectMembership.generation_id
                    == context["generation_id"],
                    models.CurrentTaskProjectMembership.task_id == imported.task_id,
                )
            )
            assert membership is not None
            membership.is_member = False
            session.flush()


def test_command_content_binding_must_match_its_execution(core_db) -> None:
    factory, ids = core_db
    with pytest.raises(IntegrityError, match="content creation receipt mismatch"):
        with session_scope(factory) as session:
            context = _bootstrap_registry(session, ids)
            imported = _import_one(session, ids, context)
            state = session.get(
                models.DishState, (context["generation_id"], imported.task_id)
            )
            assert state is not None
            mismatched_binding_id = _next(ids)
            ContractBindingRepository(session).add(
                models.HonestContractBinding(
                    binding_id=mismatched_binding_id,
                    binding_kind="task_schema",
                    source_identity="honest-pantry@task-schema-mismatch",
                    dish_release="dish-42619b9",
                    honest_release="honest-1",
                    protocol_release="protocol-1",
                    protocol_sha256="d" * 64,
                    schema_release="schema-mismatch",
                    schema_sha256="e" * 64,
                    migration_id=None,
                    source_schema_version=None,
                    target_schema_version=None,
                    migration_metadata_sha256=None,
                    source_ids={"fixture": "binding-mismatch"},
                    provenance={"fixture": True},
                    resolved_at=NOW,
                )
            )
            run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
            session.add(
                wf.ServiceRun(
                    run_id=run_id,
                    generation_id=context["generation_id"],
                    owner_id="binding-test",
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
                    owner_id="binding-test",
                    principal_class="service",
                    command_name="binding-test",
                    canonical_payload_sha256="f" * 64,
                    canonical_payload={"fixture": "binding-test"},
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
                    command_name="binding-test",
                    transaction_profile="L",
                    canonical_intent={"fixture": "binding-test"},
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
            session.flush()
            session.add(
                models.DishMutationReceipt(
                    generation_id=context["generation_id"],
                    task_id=imported.task_id,
                    dish_version=2,
                    source_route="command_execution",
                    import_run_id=None,
                    command_execution_id=execution_id,
                    content_changed=True,
                    placement_changed=False,
                    completion_changed=False,
                    occurred_at=NOW,
                )
            )
            session.flush()
            session.add(
                models.ContentVersion(
                    content_version_id=_next(ids),
                    generation_id=context["generation_id"],
                    task_id=imported.task_id,
                    representation_kind="document",
                    title="Wrong binding",
                    body="Wrong binding\n---\nStatus: ready\n",
                    identity_scheme="legacy-sha256-v1",
                    content_identity="f" * 64,
                    creator_route="command_execution",
                    import_run_id=None,
                    command_execution_id=execution_id,
                    predecessor_content_version_id=state.current_content_version_id,
                    contract_binding_id=mismatched_binding_id,
                    created_dish_version=2,
                    created_at=NOW,
                )
            )
            session.flush()


def test_command_content_binding_cannot_drift_after_insert(core_db) -> None:
    factory, ids = core_db
    with pytest.raises(IntegrityError, match="command content binding is immutable"):
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
            replacement_binding_id = _next(ids)
            ContractBindingRepository(session).add(
                models.HonestContractBinding(
                    binding_id=replacement_binding_id,
                    binding_kind="task_schema",
                    source_identity="honest-pantry@binding-drift",
                    dish_release="dish-42619b9",
                    honest_release="honest-1",
                    protocol_release="protocol-1",
                    protocol_sha256="d" * 64,
                    schema_release="schema-binding-drift",
                    schema_sha256="e" * 64,
                    migration_id=None,
                    source_schema_version=None,
                    target_schema_version=None,
                    migration_metadata_sha256=None,
                    source_ids={"fixture": "binding-drift"},
                    provenance={"fixture": True},
                    resolved_at=NOW,
                )
            )
            run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
            session.add(
                wf.ServiceRun(
                    run_id=run_id,
                    generation_id=context["generation_id"],
                    owner_id="binding-drift-test",
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
                    owner_id="binding-drift-test",
                    principal_class="service",
                    command_name="binding-drift-test",
                    canonical_payload_sha256="f" * 64,
                    canonical_payload={"fixture": "binding-drift"},
                    protocol_release="protocol-1",
                    dish_release="dish-42619b9",
                    admitted_at=NOW,
                )
            )
            session.flush()
            execution = wf.CommandExecution(
                execution_id=execution_id,
                generation_id=context["generation_id"],
                request_id=request_id,
                task_id=imported.task_id,
                operation_id=None,
                command_name="binding-drift-test",
                transaction_profile="L",
                canonical_intent={"fixture": "binding-drift"},
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
            session.add(execution)
            session.flush()
            mutation = DishRepository(
                session, uuid_factory=lambda: _next(ids)
            ).begin_scalar_mutation(
                generation_id=context["generation_id"],
                task_id=imported.task_id,
                expected_dish_version=state.dish_version,
                expected_membership_revision=head.membership_revision,
                source=ScalarMutationSource(
                    route="command_execution",
                    command_execution_id=execution_id,
                    occurred_at=NOW + timedelta(seconds=1),
                ),
            )
            mutation.replace_content(
                title="Binding-stable content",
                body="Binding-stable content\n---\nStatus: ready\n",
                identity_scheme="legacy-sha256-v1",
                content_identity="f" * 64,
                contract_binding_id=context["binding_id"],
                predecessor_content_version_id=state.current_content_version_id,
            )
            mutation.finalize()
            session.flush()
            execution.contract_binding_id = replacement_binding_id
            session.flush()


def test_abandonment_baseline_content_fk_is_generation_and_task_exact() -> None:
    constraint = next(
        item
        for item in wf.AbandonmentAttempt.__table__.foreign_key_constraints
        if item.name == "fk_abandonment_exact_baseline_content"
    )
    assert tuple(column.name for column in constraint.columns) == (
        "generation_id",
        "task_id",
        "baseline_content_version_id",
    )


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
