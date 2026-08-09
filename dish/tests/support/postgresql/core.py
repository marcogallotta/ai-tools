"""Core PostgreSQL authority fixtures shared across collected tests."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.repositories import AuthorityRepository, ContractBindingRepository, RegistryRepository
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from tests.support.postgresql.certification import postgresql_dsn

ROOT = Path(__file__).resolve().parents[3]

NOW = datetime(2026, 8, 1, 19, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64

def _uuid_stream() -> Iterator[uuid.UUID]:
    for value in range(1, 1000):
        yield uuid.UUID(int=value)


def _alembic_config(dsn: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def _reset_postgresql_schema(dsn: str) -> None:
    """Reset a disposable PostgreSQL test schema and migrate it through Alembic head."""
    reset_engine = create_engine(dsn, future=True)
    try:
        with reset_engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        reset_engine.dispose()
    command.upgrade(_alembic_config(dsn), "head")


@pytest.fixture
def core_db(request) -> tuple[sessionmaker[Session], Iterator[uuid.UUID]]:
    if request.config.getoption("--postgresql"):
        # The native lane must exercise the deployed migration history, including hand-written
        # PostgreSQL triggers that cannot be produced by ORM metadata.create_all().
        request.getfixturevalue("native_postgresql_identity")
        dsn = postgresql_dsn()
        _reset_postgresql_schema(dsn)
        engine = create_engine(dsn, future=True)
        assert engine.dialect.name == "postgresql"
    else:
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
    schema_head: str = "0002_core_authority_model",
    section_display_name: str = "Research Queue",
    section_workflow_role: str = "research_queue",
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
            schema_head=schema_head,
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
                display_name=section_display_name,
                workflow_role=section_workflow_role,
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
