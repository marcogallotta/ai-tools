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
from dish_pg.native_catalog_runtime_finalizer import (
    FINALIZER_REVISION,
    NativeCatalogRuntimeFinalizerError,
    finalize_native_catalog_runtime_authority,
)
from dish_pg.native_section_carry_forward import RepositoryIdentity
from dish_pg.native_section_content_materializer import (
    NativeSectionContentMaterializationError,
    materialize_staged_native_section_content,
)
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.repositories import (
    AuthorityRepository,
    CatalogRepository,
    ContractBindingRepository,
    CoreAuthorityError,
    RegistryRepository,
)
from tests.postgresql.test_native_section_content_materializer import _stage_pr3
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

    # PR2a remains the schema-only migration boundary. 0050 is the online
    # authority finalizer and deliberately refuses an active generation that has
    # not satisfied the reviewed 0048 inventory/materialization prerequisite.
    command.upgrade(config, "0049_native_catalog_runtime_authority_root")
    engine = create_engine(database_url, future=True)
    try:
        assert "native_catalog_runtime_attestations" in inspect(engine).get_table_names()
        assert "current_native_catalog_runtimes" in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(models.NativeCatalogRuntimeAttestation)
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(models.CurrentNativeCatalogRuntime)
            ) == 0
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
    assert "CREATE TABLE native_catalog_runtime_attestations" in rendered
    assert "CREATE TABLE current_native_catalog_runtimes" in rendered
    assert "INSERT INTO native_catalog_runtime_attestations" not in rendered
    assert "INSERT INTO current_native_catalog_runtimes" not in rendered


def test_pr2a_attestation_hash_has_fixed_vector() -> None:
    value = models.compute_attestation_sha256(
        generation_id=uuid.UUID(int=1),
        catalog_version_id=uuid.UUID(int=2),
        catalog_activation_id=uuid.UUID(int=3),
        contract_binding_id=uuid.UUID(int=4),
        attestation_revision=1,
        predecessor_attestation_id=None,
        baseline_migration_event_id=uuid.UUID(int=5),
        baseline_revision="0049",
        baseline_migration_code_sha256="a" * 64,
        baseline_dish_release="dish-test",
        baseline_source_commit_sha="b" * 40,
    )
    assert value == "d0a12fc94bdb1000a1b47bea0b83768ffa3eb93e4901b69463969215c3e6826e"


def test_pr2a_schema_only_upgrade_adds_spine_without_rows(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'pr2a.sqlite3'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, ALEMBIC_HEAD)

    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        assert {
            "native_catalog_runtime_attestations",
            "current_native_catalog_runtimes",
        }.issubset(inspector.get_table_names())
        assert "catalog_version_id" in {
            column["name"] for column in inspector.get_columns("dish_states")
        }
        assert {
            "expected_placement_version",
            "catalog_version_id",
        }.issubset(
            column["name"] for column in inspector.get_columns("task_execution_fences")
        )
        assert "catalog_version_id" in {
            column["name"] for column in inspector.get_columns("workflow_operations")
        }
        assert "catalog_version_id" in {
            column["name"]
            for column in inspector.get_columns("verification_inspection_occurrences")
        }
        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(models.NativeCatalogRuntimeAttestation)
            ) == 0
            assert session.scalar(
                select(func.count()).select_from(models.CurrentNativeCatalogRuntime)
            ) == 0
    finally:
        engine.dispose()


def test_pr2a_offline_sql_contains_no_runtime_authority_rows() -> None:
    buffer = io.StringIO()
    offline_config = Config(str(ROOT / "alembic.ini"))
    offline_config.set_main_option("sqlalchemy.url", "postgresql://offline/pr2a")
    offline_config.attributes["output_buffer"] = buffer
    command.upgrade(
        offline_config,
        "0048_native_section_content_carry_forward:0049_native_catalog_runtime_authority_root",
        sql=True,
    )
    rendered = buffer.getvalue()
    assert "CREATE TABLE native_catalog_runtime_attestations" in rendered
    assert "CREATE TABLE current_native_catalog_runtimes" in rendered
    assert "INSERT INTO native_catalog_runtime_attestations" not in rendered
    assert "INSERT INTO current_native_catalog_runtimes" not in rendered


def _seed_pr2a_attestation(
    factory, ids: Iterator[uuid.UUID]
) -> dict[str, uuid.UUID]:
    with session_scope(factory) as session:
        generation_id, binding_id = _add_native_generation(session, ids)
        section_id = _next(ids)
        CatalogRepository(session).add_section(
            models.Section(
                section_id=section_id,
                logical_name="PR2a Section",
                lifecycle="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        active = _install_recovery_catalog(
            session,
            ids,
            generation_id=generation_id,
            binding_id=binding_id,
            section_ids=(section_id,),
            revision=1,
            expected=(None, None, None),
        )
        event_id = _next(ids)
        session.add(
            models.AppliedMigrationEvent(
                migration_event_id=event_id,
                generation_id=generation_id,
                revision="later-pr2f-switch",
                predecessor_revision=ALEMBIC_HEAD,
                migration_code_sha256="c" * 64,
                dish_release="dish-native-catalog",
                initiator="test",
                outcome="applied",
                started_at=NOW,
                terminal_at=NOW,
                details={"source_commit_sha": "d" * 40},
            )
        )
        attestation_id = _next(ids)
        session.add(
            models.NativeCatalogRuntimeAttestation(
                attestation_id=attestation_id,
                generation_id=generation_id,
                catalog_version_id=active.catalog_version.catalog_version_id,
                catalog_activation_id=active.catalog_activation.catalog_activation_id,
                predecessor_attestation_id=None,
                baseline_migration_event_id=event_id,
                attestation_revision=1,
                attestation_sha256=models.compute_attestation_sha256(
                    generation_id=generation_id,
                    catalog_version_id=active.catalog_version.catalog_version_id,
                    catalog_activation_id=active.catalog_activation.catalog_activation_id,
                    contract_binding_id=binding_id,
                    attestation_revision=1,
                    predecessor_attestation_id=None,
                    baseline_migration_event_id=event_id,
                    baseline_revision="later-pr2f-switch",
                    baseline_migration_code_sha256="c" * 64,
                    baseline_dish_release="dish-native-catalog",
                    baseline_source_commit_sha="d" * 40,
                ),
                recorded_at=NOW,
            )
        )
        return {
            "generation": generation_id,
            "catalog": active.catalog_version.catalog_version_id,
            "activation": active.catalog_activation.catalog_activation_id,
            "attestation": attestation_id,
        }


def test_pr2a_pointer_constraints_and_read_only_resolution(core_db) -> None:
    factory, ids = core_db
    values = _seed_pr2a_attestation(factory, ids)

    with factory() as session:
        session.add(
            models.CurrentNativeCatalogRuntime(
                generation_id=values["generation"],
                attestation_id=values["attestation"],
                catalog_version_id=uuid.UUID(int=999),
                catalog_activation_id=values["activation"],
                attestation_revision=1,
                updated_at=NOW,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    with session_scope(factory) as session:
        assert models.resolve_current_native_catalog_runtime(
            session, values["generation"]
        ) is None
        session.add(
            models.CurrentNativeCatalogRuntime(
                generation_id=values["generation"],
                attestation_id=values["attestation"],
                catalog_version_id=values["catalog"],
                catalog_activation_id=values["activation"],
                attestation_revision=1,
                updated_at=NOW,
            )
        )

    with session_scope(factory) as session:
        resolved = models.resolve_current_native_catalog_runtime(
            session, values["generation"]
        )
        assert resolved is not None
        assert resolved[0].attestation_id == values["attestation"]
        resolved[1].attestation_sha256 = "e" * 64
        with pytest.raises(IntegrityError):
            session.flush()

    with factory() as session:
        pointer = session.get(models.CurrentNativeCatalogRuntime, values["generation"])
        assert pointer is not None
        session.delete(pointer)
        with pytest.raises(IntegrityError):
            session.commit()


def test_pr2a_absent_pointer_preserves_legacy_authority(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        legacy = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
        assert models.resolve_current_native_catalog_runtime(
            session, legacy["generation_id"]
        ) is None
        contract = RegistryRepository(session).active_release_contract(
            legacy["generation_id"]
        )
        assert (
            contract.registry_version.registry_version_id
            == legacy["registry_version_id"]
        )


def _stage_runtime_switch_fixture(session, ids, monkeypatch):
    monkeypatch.setattr(
        "dish_pg.native_section_carry_forward._verified_repository_identity",
        lambda: RepositoryIdentity(commit_sha="a" * 40, tree_sha="b" * 40),
    )
    return _stage_pr3(session, ids)


def test_pr2f_atomically_materializes_and_establishes_revision_one_root(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, expectation, _, carry_event_id, occurrences = _stage_runtime_switch_fixture(
            session, ids, monkeypatch
        )
        result = finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
        )
        assert result.inserted is True
        assert result.materialization.materialized_count == len(occurrences) == 23
        assert result.materialization.already_materialized_count == 0

        event = session.get(models.AppliedMigrationEvent, result.migration_event_id)
        assert event is not None and event.revision == FINALIZER_REVISION
        assert event.predecessor_revision == "0049_native_catalog_runtime_authority_root"
        assert event.details["authority_transition"] == "native_section_runtime_root_v1"
        assert event.details["source_commit_sha"] == "f" * 40
        assert event.details["catalog_version_id"] == str(expectation.base_catalog_version_id)
        gate = event.details["inventory_gate"]
        assert gate["decision"] == "carry_forward_completed"
        assert gate["generation_id"] == str(seeded["generation_id"])
        assert gate["prerequisite_migration_event_id"] == str(carry_event_id)
        assert gate["prerequisite_migration_revision"] == "0048_native_section_content_carry_forward"
        assert gate["counts"]["legacy_destination_documents"] == 23

        resolved = models.resolve_current_native_catalog_runtime(
            session, seeded["generation_id"]
        )
        assert resolved is not None
        pointer, attestation = resolved
        assert pointer.attestation_id == result.attestation_id
        assert pointer.catalog_version_id == expectation.base_catalog_version_id
        assert pointer.attestation_revision == 1
        assert attestation.predecessor_attestation_id is None
        assert attestation.baseline_migration_event_id == event.migration_event_id
        assert attestation.attestation_sha256 == models.compute_attestation_sha256(
            generation_id=seeded["generation_id"],
            catalog_version_id=expectation.base_catalog_version_id,
            catalog_activation_id=result.catalog_activation_id,
            contract_binding_id=session.get(
                models.SectionCatalogVersion, expectation.base_catalog_version_id
            ).contract_binding_id,
            attestation_revision=1,
            predecessor_attestation_id=None,
            baseline_migration_event_id=event.migration_event_id,
            baseline_revision=event.revision,
            baseline_migration_code_sha256=event.migration_code_sha256,
            baseline_dish_release=event.dish_release,
            baseline_source_commit_sha="f" * 40,
        )
        for occurrence in occurrences:
            state = session.get(
                models.DishState, (seeded["generation_id"], occurrence.task_id)
            )
            assert state is not None
            assert state.catalog_version_id == expectation.base_catalog_version_id

    # A new session observes only the durable root and an exact retry is a no-op.
    with session_scope(factory) as session:
        retry = finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=2)
        )
        assert retry.inserted is False
        assert retry.migration_event_id == result.migration_event_id
        assert retry.attestation_id == result.attestation_id
        assert retry.materialization.materialized_count == 0
        assert retry.materialization.already_materialized_count == 23
        assert CatalogRepository(session).active_runtime_catalog_contract(
            seeded["generation_id"]
        ) is not None


def test_pr2f_failure_rolls_back_finalizer_event_root_and_pointer(
    core_db, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, _expectation, _, _carry_event_id, occurrences = _stage_runtime_switch_fixture(
            session, ids, monkeypatch
        )
        source_content_ids = {
            occurrence.task_id: occurrence.source_content_version_id
            for occurrence in occurrences
        }

    def _fail_after_materialization(*args, **kwargs):
        materialize_staged_native_section_content(*args, **kwargs)
        raise NativeSectionContentMaterializationError(
            "injected failure after staged materialization"
        )

    monkeypatch.setattr(
        "dish_pg.native_catalog_runtime_finalizer.materialize_staged_native_section_content",
        _fail_after_materialization,
    )
    with pytest.raises(
        NativeCatalogRuntimeFinalizerError,
        match="injected failure after staged materialization",
    ):
        with session_scope(factory) as session:
            finalize_native_catalog_runtime_authority(
                session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
            )

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(models.AppliedMigrationEvent).where(
                models.AppliedMigrationEvent.generation_id == seeded["generation_id"],
                models.AppliedMigrationEvent.revision == FINALIZER_REVISION,
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(models.NativeCatalogRuntimeAttestation).where(
                models.NativeCatalogRuntimeAttestation.generation_id == seeded["generation_id"]
            )
        ) == 0
        assert session.get(
            models.CurrentNativeCatalogRuntime, seeded["generation_id"]
        ) is None
        for occurrence in occurrences:
            state = session.get(
                models.DishState, (seeded["generation_id"], occurrence.task_id)
            )
            assert state is not None
            assert state.current_content_version_id == source_content_ids[occurrence.task_id]
            assert state.catalog_version_id is None


def test_pr2f_refuses_active_generation_without_same_generation_0048(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        generation_id, binding_id = _add_native_generation(session, ids)
        section_id = _next(ids)
        CatalogRepository(session).add_section(
            models.Section(
                section_id=section_id,
                logical_name="PR2f Section",
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
        with pytest.raises(
            NativeCatalogRuntimeFinalizerError,
            match="same-generation applied 0048",
        ):
            finalize_native_catalog_runtime_authority(
                session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
            )
        assert session.get(models.CurrentNativeCatalogRuntime, generation_id) is None
