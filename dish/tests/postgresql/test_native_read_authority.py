from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.read_model import ReadModelError
from dish_pg.repositories import CatalogRepository
from dish_tool.content_versions import content_identity
from tests.support.postgresql.command import _call, _port
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _install_native_read_authority(
    session,
    ids: Iterator[uuid.UUID],
    context: dict[str, uuid.UUID],
    *,
    section_ids: tuple[uuid.UUID, ...],
    attestation_hash: str | None = None,
) -> dict[str, uuid.UUID]:
    catalog = CatalogRepository(session)
    for index, section_id in enumerate(section_ids):
        if session.get(models.Section, section_id) is None:
            catalog.add_section(
                models.Section(
                    section_id=section_id,
                    logical_name=f"native-section-{index}",
                    lifecycle="active",
                    created_at=NOW,
                    retired_at=None,
                )
            )

    catalog_version_id = _next(ids)
    catalog_activation_id = _next(ids)
    active = catalog.install_catalog_revision(
        version=models.SectionCatalogVersion(
            catalog_version_id=catalog_version_id,
            generation_id=context["generation_id"],
            version_number=1,
            contract_binding_id=context["binding_id"],
            catalog_sha256="a" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW,
        ),
        entries=tuple(
            models.SectionCatalogEntry(
                catalog_version_id=catalog_version_id,
                section_id=section_id,
                ordinal=index,
                display_name=f"Native Section {index}",
                workflow_role=f"native_role_{index}",
            )
            for index, section_id in enumerate(section_ids)
        ),
        activation=models.SectionCatalogActivation(
            catalog_activation_id=catalog_activation_id,
            generation_id=context["generation_id"],
            catalog_version_id=catalog_version_id,
            activation_route="recovery",
            import_run_id=None,
            command_execution_id=None,
            catalog_revision=1,
            activated_at=NOW,
        ),
        expected_catalog_version_id=None,
        expected_catalog_activation_id=None,
        expected_catalog_revision=None,
    )
    event_id = _next(ids)
    session.add(
        models.AppliedMigrationEvent(
            migration_event_id=event_id,
            generation_id=context["generation_id"],
            revision="0050_native_runtime_switch_test",
            predecessor_revision="0049_native_runtime_authority_spine",
            migration_code_sha256="d" * 64,
            dish_release="dish-42619b9",
            initiator="test",
            outcome="applied",
            started_at=NOW,
            terminal_at=NOW,
            details={
                "authority_transition": "native_section_runtime_root_v1",
                "source_commit_sha": "e" * 40,
                "catalog_activation_id": str(catalog_activation_id),
                "catalog_version_id": str(catalog_version_id),
                "honest_contract_binding_id": str(context["binding_id"]),
                "inventory_gate": {
                    "decision": "carry_forward_completed",
                    "generation_id": str(context["generation_id"]),
                },
            },
        )
    )
    session.flush()
    attestation_id = _next(ids)
    computed_hash = models.compute_attestation_sha256(
        generation_id=context["generation_id"],
        catalog_version_id=catalog_version_id,
        catalog_activation_id=catalog_activation_id,
        contract_binding_id=context["binding_id"],
        attestation_revision=1,
        predecessor_attestation_id=None,
        baseline_migration_event_id=event_id,
        baseline_revision="0050_native_runtime_switch_test",
        baseline_migration_code_sha256="d" * 64,
        baseline_dish_release="dish-42619b9",
        baseline_source_commit_sha="e" * 40,
    )
    session.add(
        models.NativeCatalogRuntimeAttestation(
            attestation_id=attestation_id,
            generation_id=context["generation_id"],
            catalog_version_id=catalog_version_id,
            catalog_activation_id=catalog_activation_id,
            predecessor_attestation_id=None,
            baseline_migration_event_id=event_id,
            attestation_revision=1,
            attestation_sha256=attestation_hash or computed_hash,
            recorded_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.CurrentNativeCatalogRuntime(
            generation_id=context["generation_id"],
            attestation_id=attestation_id,
            catalog_version_id=catalog_version_id,
            catalog_activation_id=catalog_activation_id,
            attestation_revision=1,
            updated_at=NOW,
        )
    )
    session.flush()
    return {
        "catalog_version_id": active.catalog_version.catalog_version_id,
        "catalog_activation_id": active.catalog_activation.catalog_activation_id,
        "attestation_id": attestation_id,
        "event_id": event_id,
    }


def _add_native_task(
    session,
    ids: Iterator[uuid.UUID],
    context: dict[str, uuid.UUID],
    *,
    section_id: uuid.UUID,
    catalog_version_id: uuid.UUID,
    title: str,
) -> uuid.UUID:
    task_id = _next(ids)
    content_version_id = _next(ids)
    body = "Native read fixture\n"
    session.add(
        models.DishTask(
            task_id=task_id,
            existence_state="isolated",
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
            import_run_id=context["import_run_id"],
            command_execution_id=None,
            content_changed=True,
            placement_changed=True,
            completion_changed=True,
            archive_changed=False,
            occurred_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.ContentVersion(
            content_version_id=content_version_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            representation_kind="document",
            title=title,
            body=body,
            identity_scheme="canonical-title-body-sha256-v1",
            content_identity=content_identity(title, body),
            creator_route="import",
            import_run_id=context["import_run_id"],
            command_execution_id=None,
            predecessor_content_version_id=None,
            contract_binding_id=context["binding_id"],
            created_dish_version=1,
            created_at=NOW,
        )
    )
    session.add(
        models.DishState(
            generation_id=context["generation_id"],
            task_id=task_id,
            current_content_version_id=content_version_id,
            section_id=section_id,
            registry_version_id=context["registry_version_id"],
            catalog_version_id=catalog_version_id,
            completed=False,
            completion_reason="imported",
            archived_at=None,
            dish_version=1,
            placement_version=1,
            completion_version=1,
            updated_at=NOW,
        )
    )
    session.flush()
    return task_id


def test_native_pointer_moves_ordinary_reads_to_native_catalog(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        second_section_id = _next(ids)
        session.add(
            models.GovernedSection(
                section_id=second_section_id,
                project_id=context["project_id"],
                logical_name="Native read destination",
                lifecycle="active",
                import_run_id=context["import_run_id"],
                created_at=NOW,
                retired_at=None,
            )
        )
        session.add(
            models.SectionRegistryEntry(
                registry_version_id=context["registry_version_id"],
                section_id=second_section_id,
                ordinal=1,
                display_name="Native read destination",
                workflow_role="destination",
            )
        )
        session.flush()
        native = _install_native_read_authority(
            session,
            ids,
            context,
            section_ids=(context["section_id"], second_section_id),
        )
        task_ids = tuple(
            _add_native_task(
                session,
                ids,
                context,
                section_id=second_section_id,
                catalog_version_id=native["catalog_version_id"],
                title=f"Native task {index}",
            )
            for index in range(3)
        )
        port = _port(session, ids)

        sections = port.reads.sections()
        assert [row["section_id"] for row in sections] == [str(context["section_id"]), str(second_section_id)]
        assert {row["catalog_version_id"] for row in sections} == {str(native["catalog_version_id"])}
        assert {row["runtime_attestation_id"] for row in sections} == {str(native["attestation_id"])}

        first = port.reads.section_tasks(
            section_reference=second_section_id, page_size=2
        )
        assert [item.task_id for item in first.items] == list(task_ids[:2])
        assert first.read_authority["catalog_version_id"] == str(
            native["catalog_version_id"]
        )
        assert first.next_cursor is not None
        cursor_payload = port.reads.cursor_codec.decode(first.next_cursor)
        assert cursor_payload["runtime_attestation_id"] == str(native["attestation_id"])
        second = port.reads.section_tasks(
            section_reference=second_section_id,
            page_size=2,
            cursor=first.next_cursor,
        )
        assert [item.task_id for item in second.items] == [task_ids[2]]

        search = port.reads.native_search(query="Native task", page_size=2, cursor=None)
        assert search is not None
        assert search["catalog_version_id"] == str(native["catalog_version_id"])
        assert search["runtime_attestation_id"] == str(native["attestation_id"])
        assert [row["dish_id"] for row in search["results"]] == [
            str(value) for value in task_ids[:2]
        ]
        assert search["next_cursor"] is not None
        second_search = port.reads.native_search(
            query="Native task", page_size=2, cursor=search["next_cursor"]
        )
        assert second_search is not None
        assert [row["dish_id"] for row in second_search["results"]] == [str(task_ids[2])]

        read = port.execute(
            _call("read", run_id=_next(ids), arguments={"dish_id": str(task_ids[0])})
        )
        assert read.ok
        assert read.data["section_id"] == str(second_section_id)
        assert read.data["catalog_version_id"] == str(native["catalog_version_id"])
        assert read.data["runtime_attestation_id"] == str(native["attestation_id"])
        assert read.data["membership_revision"] is None


def test_native_reads_do_not_fall_back_after_transition_witness(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        session.add(
            models.AppliedMigrationEvent(
                migration_event_id=_next(ids),
                generation_id=context["generation_id"],
                revision="0050_native_runtime_switch_test",
                predecessor_revision="0049_native_runtime_authority_spine",
                migration_code_sha256="d" * 64,
                dish_release="dish-42619b9",
                initiator="test",
                outcome="applied",
                started_at=NOW,
                terminal_at=NOW,
                details={
                    "authority_transition": "native_section_runtime_root_v1",
                    "source_commit_sha": "e" * 40,
                },
            )
        )
        session.flush()
        port = _port(session, ids)

        with pytest.raises(
            ReadModelError, match="native runtime root is missing after the authority transition"
        ):
            port.reads.sections()


def test_native_reads_fail_closed_when_catalog_moves_past_runtime_pointer(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        native = _install_native_read_authority(
            session,
            ids,
            context,
            section_ids=(context["section_id"],),
        )
        next_catalog_version_id = _next(ids)
        CatalogRepository(session).install_catalog_revision(
            version=models.SectionCatalogVersion(
                catalog_version_id=next_catalog_version_id,
                generation_id=context["generation_id"],
                version_number=2,
                contract_binding_id=context["binding_id"],
                catalog_sha256="b" * 64,
                source_registry_version_id=None,
                transform_sha256=None,
                created_at=NOW,
            ),
            entries=(
                models.SectionCatalogEntry(
                    catalog_version_id=next_catalog_version_id,
                    section_id=context["section_id"],
                    ordinal=0,
                    display_name="Research Queue",
                    workflow_role="research_queue",
                ),
            ),
            activation=models.SectionCatalogActivation(
                catalog_activation_id=_next(ids),
                generation_id=context["generation_id"],
                catalog_version_id=next_catalog_version_id,
                activation_route="recovery",
                import_run_id=None,
                command_execution_id=None,
                catalog_revision=2,
                activated_at=NOW,
            ),
            expected_catalog_version_id=native["catalog_version_id"],
            expected_catalog_activation_id=native["catalog_activation_id"],
            expected_catalog_revision=1,
        )
        with pytest.raises(ReadModelError, match="stale against the active catalog"):
            _port(session, ids).reads.sections()


def test_native_reads_validate_runtime_attestation_hash(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    with session_scope(factory) as session:
        _install_native_read_authority(
            session,
            ids,
            context,
            section_ids=(context["section_id"],),
            attestation_hash="0" * 64,
        )
        with pytest.raises(ReadModelError, match="attestation hash is inconsistent"):
            _port(session, ids).reads.sections()
