from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.repositories import (
    AuthorityRepository,
    CatalogRepository,
    ContractBindingRepository,
    CoreAuthorityError,
    RegistryRepository,
)
from tests.support.postgresql.core import _bootstrap_registry, _next

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


def _add_native_generation(
    session: Session, ids: Iterator[uuid.UUID]
) -> tuple[uuid.UUID, uuid.UUID]:
    generation_id = _next(ids)
    binding_id = _next(ids)
    AuthorityRepository(session).add_generation(
        models.AuthorityGeneration(
            generation_id=generation_id,
            predecessor_generation_id=None,
            creation_reason="initial_cutover",
            external_restore_control_id=None,
            schema_head=ALEMBIC_HEAD,
            dish_release="dish-native-catalog",
            status="active",
            created_at=NOW,
            retired_at=None,
        )
    )
    ContractBindingRepository(session).add(
        models.HonestContractBinding(
            binding_id=binding_id,
            binding_kind="release",
            source_identity="honest-pantry@native-catalog",
            dish_release="dish-native-catalog",
            honest_release="honest-native-catalog",
            protocol_release="protocol-native-catalog",
            protocol_sha256=HASH_A,
            schema_release="schema-native-catalog",
            schema_sha256=HASH_B,
            migration_id=None,
            source_schema_version=None,
            target_schema_version=None,
            migration_metadata_sha256=None,
            source_ids={"repo": "honest-pantry"},
            provenance={"resolved_by": "native-catalog-test"},
            resolved_at=NOW,
        )
    )
    return generation_id, binding_id


def _install_recovery_catalog(
    session: Session,
    ids: Iterator[uuid.UUID],
    *,
    generation_id: uuid.UUID,
    binding_id: uuid.UUID,
    section_ids: tuple[uuid.UUID, ...],
    revision: int,
    expected: tuple[uuid.UUID | None, uuid.UUID | None, int | None],
):
    version_id = _next(ids)
    activation_id = _next(ids)
    return CatalogRepository(session).install_catalog_revision(
        version=models.SectionCatalogVersion(
            catalog_version_id=version_id,
            generation_id=generation_id,
            version_number=revision,
            contract_binding_id=binding_id,
            catalog_sha256=f"{revision:064x}",
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW + timedelta(minutes=revision),
        ),
        entries=(
            models.SectionCatalogEntry(
                catalog_version_id=version_id,
                section_id=section_id,
                ordinal=ordinal,
                display_name=f"Native Section {ordinal}",
                workflow_role=f"native_role_{ordinal}",
            )
            for ordinal, section_id in enumerate(section_ids)
        ),
        activation=models.SectionCatalogActivation(
            catalog_activation_id=activation_id,
            generation_id=generation_id,
            catalog_version_id=version_id,
            activation_route="recovery",
            import_run_id=None,
            command_execution_id=None,
            catalog_revision=revision,
            activated_at=NOW + timedelta(minutes=revision),
        ),
        expected_catalog_version_id=expected[0],
        expected_catalog_activation_id=expected[1],
        expected_catalog_revision=expected[2],
    )


def test_native_catalog_is_current_without_project_or_asana_topology(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        generation_id, binding_id = _add_native_generation(session, ids)
        section_ids = (_next(ids), _next(ids))
        catalog = CatalogRepository(session)
        for ordinal, section_id in enumerate(section_ids):
            catalog.add_section(
                models.Section(
                    section_id=section_id,
                    logical_name=f"native-section-{ordinal}",
                    lifecycle="active",
                    created_at=NOW,
                    retired_at=None,
                )
            )

        first = _install_recovery_catalog(
            session,
            ids,
            generation_id=generation_id,
            binding_id=binding_id,
            section_ids=section_ids,
            revision=1,
            expected=(None, None, None),
        )
        second = _install_recovery_catalog(
            session,
            ids,
            generation_id=generation_id,
            binding_id=binding_id,
            section_ids=section_ids,
            revision=2,
            expected=(
                first.catalog_version.catalog_version_id,
                first.catalog_activation.catalog_activation_id,
                1,
            ),
        )

        assert second.active_catalog.catalog_revision == 2
        assert [entry.section_id for entry in second.entries] == list(section_ids)
        assert second.honest_binding.binding_id == binding_id
        assert (
            session.scalar(select(func.count()).select_from(models.GovernedProject))
            == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(models.GovernedSection))
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(models.SectionExternalAlias)
            )
            == 0
        )

        with pytest.raises(CoreAuthorityError, match="compare-and-swap input is stale"):
            _install_recovery_catalog(
                session,
                ids,
                generation_id=generation_id,
                binding_id=binding_id,
                section_ids=section_ids,
                revision=3,
                expected=(
                    first.catalog_version.catalog_version_id,
                    first.catalog_activation.catalog_activation_id,
                    1,
                ),
            )
        assert (
            CatalogRepository(session)
            .active_catalog_contract(generation_id)
            .catalog_version.catalog_version_id
            == second.catalog_version.catalog_version_id
        )


def test_active_catalog_section_cannot_be_retired(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        generation_id, binding_id = _add_native_generation(session, ids)
        section_id = _next(ids)
        CatalogRepository(session).add_section(
            models.Section(
                section_id=section_id,
                logical_name="native-section",
                lifecycle="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        _install_recovery_catalog(
            session,
            ids,
            generation_id=generation_id,
            binding_id=binding_id,
            section_ids=(section_id,),
            revision=1,
            expected=(None, None, None),
        )

    with (
        pytest.raises(IntegrityError, match="active catalog Section cannot be retired"),
        session_scope(factory) as session,
    ):
        section = session.get(models.Section, section_id)
        assert section is not None
        section.lifecycle = "retired"
        section.retired_at = NOW + timedelta(hours=1)
        session.flush()


def test_database_rejects_non_root_initial_catalog_pointer(core_db) -> None:
    factory, ids = core_db
    with (
        pytest.raises(IntegrityError, match="active native catalog"),
        session_scope(factory) as session,
    ):
        generation_id, binding_id = _add_native_generation(session, ids)
        section_id = _next(ids)
        version_id = _next(ids)
        activation_id = _next(ids)
        CatalogRepository(session).add_section(
            models.Section(
                section_id=section_id,
                logical_name="native-section",
                lifecycle="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        session.add(
            models.SectionCatalogVersion(
                catalog_version_id=version_id,
                generation_id=generation_id,
                version_number=2,
                contract_binding_id=binding_id,
                catalog_sha256=HASH_A,
                source_registry_version_id=None,
                transform_sha256=None,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            models.SectionCatalogEntry(
                catalog_version_id=version_id,
                section_id=section_id,
                ordinal=0,
                display_name="Native Section",
                workflow_role="native_section",
            )
        )
        session.flush()
        session.add(
            models.SectionCatalogActivation(
                catalog_activation_id=activation_id,
                generation_id=generation_id,
                catalog_version_id=version_id,
                activation_route="recovery",
                import_run_id=None,
                command_execution_id=None,
                catalog_revision=2,
                activated_at=NOW,
            )
        )
        session.flush()
        session.add(
            models.ActiveSectionCatalog(
                generation_id=generation_id,
                catalog_version_id=version_id,
                catalog_activation_id=activation_id,
                catalog_revision=2,
                updated_at=NOW,
            )
        )
        session.flush()


def test_legacy_registry_remains_runtime_contract_without_native_catalog(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
        contract = RegistryRepository(session).active_release_contract(
            context["generation_id"]
        )
        assert (
            contract.registry_version.registry_version_id
            == context["registry_version_id"]
        )
        assert (
            session.get(models.ActiveSectionCatalog, context["generation_id"]) is None
        )


def test_populated_upgrade_backfills_definition_authority_only(tmp_path: Path) -> None:
    database_path = tmp_path / "native-catalog.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "0045_cook_log_entries")

    engine = create_engine(database_url, future=True)
    factory = sessionmaker(bind=engine, class_=Session, future=True)
    ids = (uuid.UUID(int=value) for value in range(1, 1000))
    try:
        with session_scope(factory) as session:
            legacy = _bootstrap_registry(
                session,
                ids,
                generation_status="active",
                schema_head="0045_cook_log_entries",
            )
    finally:
        engine.dispose()

    command.upgrade(config, ALEMBIC_HEAD)
    engine = create_engine(database_url, future=True)
    try:
        assert (
            "native_catalog_runtime_attestations"
            not in inspect(engine).get_table_names()
        )
        assert (
            "current_native_catalog_runtimes" not in inspect(engine).get_table_names()
        )
        with Session(engine) as session:
            section = session.get(models.Section, legacy["section_id"])
            version = session.get(
                models.SectionCatalogVersion, legacy["registry_version_id"]
            )
            active_catalog = session.get(
                models.ActiveSectionCatalog, legacy["generation_id"]
            )
            active_registry = session.get(
                models.ActiveSectionRegistry, legacy["generation_id"]
            )
            assert section is not None and section.logical_name == "Research Queue"
            assert version is not None
            assert version.source_registry_version_id == legacy["registry_version_id"]
            assert active_catalog is not None
            assert active_catalog.catalog_version_id == legacy["registry_version_id"]
            assert active_registry is not None
            assert active_registry.registry_version_id == legacy["registry_version_id"]
    finally:
        engine.dispose()


def test_downgrade_refuses_to_discard_native_section_identity(tmp_path: Path) -> None:
    database_path = tmp_path / "native-section-downgrade.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, ALEMBIC_HEAD)

    engine = create_engine(database_url, future=True)
    factory = sessionmaker(bind=engine, class_=Session, future=True)
    ids = (uuid.UUID(int=value) for value in range(1, 1000))
    try:
        with session_scope(factory) as session:
            _generation_id, _binding_id = _add_native_generation(session, ids)
            CatalogRepository(session).add_section(
                models.Section(
                    section_id=_next(ids),
                    logical_name="native-only-section",
                    lifecycle="active",
                    created_at=NOW,
                    retired_at=None,
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(
        RuntimeError, match="downgrade refuses native Section/catalog changes"
    ):
        command.downgrade(config, "0045_cook_log_entries")


def test_offline_migration_contains_foundation_without_runtime_switch() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, ALEMBIC_HEAD, sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE sections" in rendered
    assert "CREATE TABLE section_catalog_versions" in rendered
    assert "CREATE TABLE active_section_catalogs" in rendered
    assert "dish_validate_active_section_catalog" in rendered
    assert "native_catalog_runtime_attestations" not in rendered
    assert "current_native_catalog_runtimes" not in rendered
