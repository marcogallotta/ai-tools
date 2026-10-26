"""Shared native catalog read-authority test fixture."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from dish_pg import models
from dish_pg.repositories import CatalogRepository
from tests.support.postgresql.workflow import NOW, _next


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


