from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.repositories import (
    AuthorityRepository,
    ContractBindingRepository,
    CoreAuthorityError,
    RegistryRepository,
)
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 1, 19, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _uuid_stream() -> Iterator[uuid.UUID]:
    for value in range(1, 1000):
        yield uuid.UUID(int=value)


@pytest.fixture
def core_db() -> tuple[sessionmaker[Session], Iterator[uuid.UUID]]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    models.Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    ids = _uuid_stream()
    yield factory, ids
    engine.dispose()


def _next(ids: Iterator[uuid.UUID]) -> uuid.UUID:
    return next(ids)


def _bootstrap_registry(
    session: Session,
    ids: Iterator[uuid.UUID],
    *,
    generation_status: str = "pending",
) -> dict[str, uuid.UUID]:
    authority = AuthorityRepository(session)
    contracts = ContractBindingRepository(session)
    registry = RegistryRepository(session)

    import_run_id = _next(ids)
    authority.add_import_run(
        models.ImportRun(
            import_run_id=import_run_id,
            source_commit="42619b9",
            source_release="dish-42619b9",
            legacy_generation_id="legacy-1",
            baseline_high_water_mark="asana-event-500",
            source_bundle_sha256=HASH_A,
            status="complete",
            started_at=NOW,
            completed_at=NOW,
            provenance={"capture": "fixture"},
        )
    )
    generation_id = _next(ids)
    authority.add_generation(
        models.AuthorityGeneration(
            generation_id=generation_id,
            predecessor_generation_id=None,
            creation_reason="initial_cutover",
            external_restore_control_id=None,
            schema_head="0002_core_authority_model",
            dish_release="dish-42619b9",
            status=generation_status,
            created_at=NOW,
            retired_at=None,
        )
    )
    binding_id = _next(ids)
    contracts.add(
        models.HonestContractBinding(
            binding_id=binding_id,
            binding_kind="release",
            source_identity="honest-pantry@release-1",
            dish_release="dish-42619b9",
            honest_release="honest-1",
            protocol_release="protocol-1",
            protocol_sha256=HASH_A,
            schema_release="schema-1",
            schema_sha256=HASH_B,
            migration_id=None,
            source_schema_version=None,
            target_schema_version=None,
            migration_metadata_sha256=None,
            source_ids={"repo": "honest-pantry"},
            provenance={"resolved_by": "fixture"},
            resolved_at=NOW,
        )
    )
    project_id = _next(ids)
    registry.add_project(
        models.GovernedProject(
            project_id=project_id,
            logical_name="Cooking",
            lifecycle="active",
            import_run_id=import_run_id,
            created_at=NOW,
            retired_at=None,
        )
    )
    registry.add_project_alias(
        models.ProjectExternalAlias(
            alias_id=_next(ids),
            project_id=project_id,
            external_system="asana",
            external_id="1217084805070730",
            origin="imported",
            import_run_id=import_run_id,
            projection_event_id=None,
            state="active",
            created_at=NOW,
            retired_at=None,
        )
    )
    section_id = _next(ids)
    registry.add_section(
        models.GovernedSection(
            section_id=section_id,
            project_id=project_id,
            logical_name="Research Queue",
            lifecycle="active",
            import_run_id=import_run_id,
            created_at=NOW,
            retired_at=None,
        )
    )
    registry.add_section_alias(
        models.SectionExternalAlias(
            alias_id=_next(ids),
            section_id=section_id,
            external_system="asana",
            external_id="1217084805070731",
            origin="imported",
            import_run_id=import_run_id,
            projection_event_id=None,
            state="active",
            created_at=NOW,
            retired_at=None,
        )
    )
    registry_version_id = _next(ids)
    registry.add_registry_version(
        models.SectionRegistryVersion(
            registry_version_id=registry_version_id,
            generation_id=generation_id,
            version_number=1,
            import_run_id=import_run_id,
            contract_binding_id=binding_id,
            registry_sha256=HASH_C,
            created_at=NOW,
        ),
        [
            models.SectionRegistryEntry(
                registry_version_id=registry_version_id,
                section_id=section_id,
                ordinal=0,
                display_name="Research Queue",
                workflow_role="research_queue",
            )
        ],
    )
    registry_activation_id = _next(ids)
    registry.activate_registry(
        activation=models.SectionRegistryActivation(
            registry_activation_id=registry_activation_id,
            generation_id=generation_id,
            registry_version_id=registry_version_id,
            activation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            registry_revision=1,
            activated_at=NOW,
        ),
        current=models.ActiveSectionRegistry(
            generation_id=generation_id,
            registry_version_id=registry_version_id,
            registry_activation_id=registry_activation_id,
            registry_revision=1,
            updated_at=NOW,
        ),
    )
    return {
        "import_run_id": import_run_id,
        "generation_id": generation_id,
        "binding_id": binding_id,
        "project_id": project_id,
        "section_id": section_id,
        "registry_version_id": registry_version_id,
    }


def _import_one(
    session: Session,
    ids: Iterator[uuid.UUID],
    context: dict[str, uuid.UUID],
    *,
    task_id: uuid.UUID | None = None,
    asana_gid: str = "123456789",
    section_id: uuid.UUID | None = None,
):
    service = CoreAuthorityService(session, uuid_factory=lambda: _next(ids))
    actual_task_id = task_id or _next(ids)
    return service.import_task_document(
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        spec=ImportedTaskSpec(
            task_id=actual_task_id,
            asana_task_gid=asana_gid,
            title="[ready] Exact imported task",
            body="Canonical body\n---\nStatus: ready\n",
            identity_scheme="legacy-sha256-v1",
            content_identity=HASH_A,
            project_ids=(context["project_id"],),
            section_id=section_id or context["section_id"],
            completed=False,
            observed_at=NOW,
        ),
    )


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
        "task_content_activations",
        "task_authority_heads",
        "task_project_membership_events",
        "current_task_project_memberships",
        "task_section_placement_events",
        "current_task_section_placements",
        "task_completion_events",
        "current_task_completion",
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
    assert "dish_validate_current_placement" in rendered
    assert "task_external_aliases_identity_update" in rendered


def test_stage2_alembic_upgrade_reaches_head_from_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "stage2.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "0002_core_authority_model")

    engine = create_engine(database_url, future=True)
    try:
        table_names = set(inspect(engine).get_table_names())
        assert set(models.CORE_TABLE_NAMES).issubset(table_names)
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == "0002_core_authority_model"
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
        head = session.get(
            models.TaskAuthorityHead,
            (context["generation_id"], result.task_id),
        )
        assert head is not None
        assert head.current_content_activation_id == result.content_activation_id
        assert head.task_revision == 1
        version = session.get(models.ContentVersion, result.content_version_id)
        assert version is not None
        assert version.title == "[ready] Exact imported task"
        assert version.body.endswith("Status: ready\n")
        placement = session.get(
            models.CurrentTaskSectionPlacement,
            (context["generation_id"], result.task_id),
        )
        assert placement is not None
        assert placement.section_id == context["section_id"]
        assert placement.registry_version_id == context["registry_version_id"]
        completion = session.get(
            models.CurrentTaskCompletion,
            (context["generation_id"], result.task_id),
        )
        assert completion is not None and completion.completed is False
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
            event = session.get(models.TaskCompletionEvent, result.completion_event_id)
            assert event is not None
            session.delete(event)
            session.flush()


def test_migration_and_contract_provenance_is_immutable(core_db) -> None:
    factory, ids = core_db
    migration_event_id = _next(ids)
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids)
        AuthorityRepository(session).add_migration_event(
            models.AppliedMigrationEvent(
                migration_event_id=migration_event_id,
                generation_id=context["generation_id"],
                revision="0002_core_authority_model",
                predecessor_revision="0001_stage_a_baseline",
                migration_code_sha256=HASH_A,
                dish_release="dish-42619b9",
                initiator="stage2-test",
                outcome="applied",
                started_at=NOW,
                terminal_at=NOW,
                details={"database": "fixture"},
            )
        )

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            event_row = session.get(models.AppliedMigrationEvent, migration_event_id)
            assert event_row is not None
            event_row.details = {"database": "rewritten"}
            session.flush()

    with pytest.raises(IntegrityError, match="immutable authority row"):
        with session_scope(factory) as session:
            binding = session.get(
                models.HonestContractBinding, context["binding_id"]
            )
            assert binding is not None
            binding.provenance = {"resolved_by": "rewritten"}
            session.flush()


def test_only_one_active_generation_and_restore_transition_is_bound(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="pending")
        authority = AuthorityRepository(session)
        authority.activate_generation(
            generation_id=context["generation_id"],
            activation=models.AuthorityActivation(
                activation_id=_next(ids),
                generation_id=context["generation_id"],
                import_run_id=context["import_run_id"],
                cutover_approval_id="approval-1",
                legacy_bundle_id="bundle-1",
                schema_head="0002_core_authority_model",
                dish_release="dish-42619b9",
                honest_release="honest-1",
                protocol_release="protocol-1",
                openapi_release="openapi-1",
                routing_release="route-1",
                projection_epoch=_next(ids),
                outcome="activated",
                rollback_burned_at=NOW,
                recorded_at=NOW,
            ),
            at=NOW,
        )

    with factory() as session:
        active = session.scalar(
            select(models.AuthorityGeneration).where(
                models.AuthorityGeneration.status == "active"
            )
        )
        assert active is not None and active.generation_id == context["generation_id"]

    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            session.add(
                models.AuthorityGeneration(
                    generation_id=_next(ids),
                    predecessor_generation_id=None,
                    creation_reason="initial_cutover",
                    external_restore_control_id=None,
                    schema_head="0002_core_authority_model",
                    dish_release="dish-duplicate",
                    status="active",
                    created_at=NOW,
                    retired_at=None,
                )
            )
            session.flush()
