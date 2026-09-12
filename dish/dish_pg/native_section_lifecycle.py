"""Native Section lifecycle commands over the current PostgreSQL catalog authority.

Create, rename, and retire are catalog-successor operations.  They never move a
Dish.  When a successor catalog becomes current, every current DishState is
rebound to the successor catalog through a dedicated catalog-rebind receipt so
its semantic Dish/content/placement versions remain unchanged.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from .repositories import CatalogRepository, CoreAuthorityError

LIFECYCLE_COMMANDS = frozenset({"create-section", "rename-section", "retire-section"})
PROTECTED_WORKFLOW_ROLES = frozenset({"research_queue", "verification_queue"})


class NativeSectionLifecycleError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.data = dict(data or {})


@dataclass(frozen=True)
class ExpectedCatalogView:
    catalog_version_id: uuid.UUID
    catalog_activation_id: uuid.UUID
    catalog_revision: int
    runtime_attestation_id: uuid.UUID
    runtime_attestation_revision: int


def _required_uuid(arguments: Mapping[str, Any], name: str) -> uuid.UUID:
    raw = arguments.get(name)
    if raw in {None, ""}:
        raise NativeSectionLifecycleError(
            "EXPECTED_VIEW_REQUIRED" if name.startswith("expected_") else "INVALID_ARGUMENT",
            f"{name} is required",
            http_status=400,
            data={"field": name},
        )
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise NativeSectionLifecycleError(
            "INVALID_ARGUMENT",
            f"{name} must be a UUID",
            http_status=400,
            data={"field": name},
        ) from exc


def _required_positive_int(arguments: Mapping[str, Any], name: str) -> int:
    raw = arguments.get(name)
    if isinstance(raw, bool):
        raw = None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise NativeSectionLifecycleError(
            "INVALID_ARGUMENT",
            f"{name} must be a positive integer",
            http_status=400,
            data={"field": name},
        ) from exc
    if value <= 0:
        raise NativeSectionLifecycleError(
            "INVALID_ARGUMENT",
            f"{name} must be a positive integer",
            http_status=400,
            data={"field": name},
        )
    return value


def _expected_view(arguments: Mapping[str, Any]) -> ExpectedCatalogView:
    return ExpectedCatalogView(
        catalog_version_id=_required_uuid(arguments, "expected_catalog_version_id"),
        catalog_activation_id=_required_uuid(arguments, "expected_catalog_activation_id"),
        catalog_revision=_required_positive_int(arguments, "expected_catalog_revision"),
        runtime_attestation_id=_required_uuid(
            arguments, "expected_runtime_attestation_id"
        ),
        runtime_attestation_revision=_required_positive_int(
            arguments, "expected_runtime_attestation_revision"
        ),
    )


def _required_label(arguments: Mapping[str, Any]) -> str:
    label = arguments.get("display_name")
    if not isinstance(label, str):
        raise NativeSectionLifecycleError(
            "INVALID_ARGUMENT",
            "display_name must be a string",
            http_status=400,
            data={"field": "display_name"},
        )
    label = label.strip()
    if not label or len(label) > 256:
        raise NativeSectionLifecycleError(
            "INVALID_ARGUMENT",
            "display_name must contain 1 to 256 characters",
            http_status=400,
            data={"field": "display_name"},
        )
    return label


def _catalog_sha256(
    *,
    generation_id: uuid.UUID,
    version_number: int,
    contract_binding_id: uuid.UUID,
    entries: tuple[models.SectionCatalogEntry, ...],
) -> str:
    pieces = [f"{generation_id}\n{version_number}\n{contract_binding_id}\n"]
    pieces.extend(
        f"{entry.ordinal}\t{entry.section_id}\t{entry.display_name}\t{entry.workflow_role}\n"
        for entry in sorted(entries, key=lambda item: item.ordinal)
    )
    return hashlib.sha256("".join(pieces).encode("utf-8")).hexdigest()


def _lock_current_runtime(
    session: Session,
    *,
    generation_id: uuid.UUID,
    expected: ExpectedCatalogView,
):
    generation_stmt = select(models.AuthorityGeneration).where(
        models.AuthorityGeneration.generation_id == generation_id
    )
    active_stmt = select(models.ActiveSectionCatalog).where(
        models.ActiveSectionCatalog.generation_id == generation_id
    )
    runtime_stmt = select(models.CurrentNativeCatalogRuntime).where(
        models.CurrentNativeCatalogRuntime.generation_id == generation_id
    )
    if session.get_bind().dialect.name == "postgresql":
        generation_stmt = generation_stmt.with_for_update().execution_options(
            populate_existing=True
        )
        active_stmt = active_stmt.with_for_update().execution_options(
            populate_existing=True
        )
        runtime_stmt = runtime_stmt.with_for_update().execution_options(
            populate_existing=True
        )
    generation = session.scalar(generation_stmt)
    active = session.scalar(active_stmt)
    pointer = session.scalar(runtime_stmt)
    if generation is None or generation.status != "active":
        raise NativeSectionLifecycleError(
            "NATIVE_RUNTIME_NOT_CURRENT", "active authority generation is missing"
        )
    if active is None or pointer is None:
        raise NativeSectionLifecycleError(
            "NATIVE_RUNTIME_NOT_CURRENT", "native Section runtime authority is not established"
        )
    actual = (
        active.catalog_version_id,
        active.catalog_activation_id,
        active.catalog_revision,
        pointer.attestation_id,
        pointer.attestation_revision,
    )
    wanted = (
        expected.catalog_version_id,
        expected.catalog_activation_id,
        expected.catalog_revision,
        expected.runtime_attestation_id,
        expected.runtime_attestation_revision,
    )
    if actual != wanted:
        raise NativeSectionLifecycleError(
            "STALE_CATALOG_VIEW",
            "native Section catalog/runtime view is stale",
            data={
                "catalog_version_id": str(active.catalog_version_id),
                "catalog_activation_id": str(active.catalog_activation_id),
                "catalog_revision": active.catalog_revision,
                "runtime_attestation_id": str(pointer.attestation_id),
                "runtime_attestation_revision": pointer.attestation_revision,
            },
        )
    attestation = session.get(models.NativeCatalogRuntimeAttestation, pointer.attestation_id)
    if (
        attestation is None
        or attestation.generation_id != generation_id
        or attestation.catalog_version_id != active.catalog_version_id
        or attestation.catalog_activation_id != active.catalog_activation_id
        or attestation.attestation_revision != pointer.attestation_revision
    ):
        raise NativeSectionLifecycleError(
            "NATIVE_RUNTIME_NOT_CURRENT", "native Section runtime pointer is inconsistent"
        )
    if attestation.attestation_revision > 1:
        predecessor = session.get(
            models.NativeCatalogRuntimeAttestation,
            attestation.predecessor_attestation_id,
        )
        if (
            predecessor is None
            or predecessor.generation_id != generation_id
            or predecessor.attestation_revision != attestation.attestation_revision - 1
        ):
            raise NativeSectionLifecycleError(
                "NATIVE_RUNTIME_NOT_CURRENT", "native runtime attestation lineage is gapped"
            )
    try:
        contract = CatalogRepository(session).active_runtime_catalog_contract(generation_id)
    except CoreAuthorityError as exc:
        raise NativeSectionLifecycleError(
            "NATIVE_RUNTIME_NOT_CURRENT", str(exc)
        ) from exc
    if contract is None:
        raise NativeSectionLifecycleError(
            "NATIVE_RUNTIME_NOT_CURRENT", "native Section runtime authority is not established"
        )
    return generation, contract, pointer, attestation


def _find_entry(
    entries: tuple[models.SectionCatalogEntry, ...], section_id: uuid.UUID
) -> models.SectionCatalogEntry:
    entry = next((item for item in entries if item.section_id == section_id), None)
    if entry is None:
        raise NativeSectionLifecycleError(
            "SECTION_NOT_FOUND", "Section is not active in the current native catalog", http_status=404
        )
    return entry


def apply_native_section_lifecycle(
    session: Session,
    *,
    command_name: str,
    arguments: Mapping[str, Any],
    generation: models.AuthorityGeneration,
    execution: wf.CommandExecution,
    now: datetime,
    uuid_factory: Callable[[], uuid.UUID],
) -> dict[str, Any]:
    if command_name not in LIFECYCLE_COMMANDS:
        raise NativeSectionLifecycleError(
            "INVALID_ARGUMENT", f"unsupported Section lifecycle command: {command_name}", http_status=400
        )
    if execution.task_id is not None or execution.operation_id is not None:
        raise NativeSectionLifecycleError(
            "SECTION_LIFECYCLE_SCOPE_MISMATCH",
            "Section lifecycle execution must be catalog-scoped",
        )
    expected = _expected_view(arguments)
    generation, contract, pointer, current_attestation = _lock_current_runtime(
        session, generation_id=generation.generation_id, expected=expected
    )
    prior_entries = tuple(contract.entries)
    prior_by_section = {entry.section_id: entry for entry in prior_entries}
    section_id: uuid.UUID
    display_name: str

    if command_name == "create-section":
        display_name = _required_label(arguments)
        section_id = uuid_factory()
        section = models.Section(
            section_id=section_id,
            logical_name=f"native-section-{section_id}",
            lifecycle="active",
            created_at=now,
            retired_at=None,
        )
        CatalogRepository(session).add_section(section)
        revised_specs = [
            (entry.section_id, entry.ordinal, entry.display_name, entry.workflow_role)
            for entry in prior_entries
        ]
        revised_specs.append(
            (section_id, len(prior_entries), display_name, f"native-section-{section_id}")
        )
        target_entry = None
    else:
        section_id = _required_uuid(arguments, "section_id")
        target_entry = _find_entry(prior_entries, section_id)
        display_name = (
            _required_label(arguments)
            if command_name == "rename-section"
            else target_entry.display_name
        )
        if command_name == "retire-section":
            if len(prior_entries) == 1:
                raise NativeSectionLifecycleError(
                    "SECTION_LAST_ACTIVE",
                    "last active Section cannot be retired",
                )
            resident_count = session.scalar(
                select(func.count())
                .select_from(models.DishState)
                .where(
                    models.DishState.generation_id == generation.generation_id,
                    models.DishState.section_id == section_id,
                )
            )
            if resident_count:
                raise NativeSectionLifecycleError(
                    "SECTION_NOT_EMPTY",
                    "Section cannot be retired while Dishes remain in it",
                    data={"dish_count": int(resident_count)},
                )
            if target_entry.workflow_role in PROTECTED_WORKFLOW_ROLES:
                raise NativeSectionLifecycleError(
                    "SECTION_REQUIRED_BY_WORKFLOW",
                    "workflow-required Section cannot be retired",
                    data={"workflow_role": target_entry.workflow_role},
                )
            survivors = [entry for entry in prior_entries if entry.section_id != section_id]
            revised_specs = [
                (entry.section_id, ordinal, entry.display_name, entry.workflow_role)
                for ordinal, entry in enumerate(survivors)
            ]
        else:
            revised_specs = [
                (
                    entry.section_id,
                    entry.ordinal,
                    display_name if entry.section_id == section_id else entry.display_name,
                    entry.workflow_role,
                )
                for entry in prior_entries
            ]

    next_revision = contract.active_catalog.catalog_revision + 1
    next_catalog_version_id = uuid_factory()
    next_catalog_activation_id = uuid_factory()
    next_entries = tuple(
        models.SectionCatalogEntry(
            catalog_version_id=next_catalog_version_id,
            section_id=entry_section_id,
            ordinal=ordinal,
            display_name=entry_display_name,
            workflow_role=workflow_role,
        )
        for entry_section_id, ordinal, entry_display_name, workflow_role in revised_specs
    )
    version = models.SectionCatalogVersion(
        catalog_version_id=next_catalog_version_id,
        generation_id=generation.generation_id,
        version_number=next_revision,
        contract_binding_id=contract.catalog_version.contract_binding_id,
        catalog_sha256=_catalog_sha256(
            generation_id=generation.generation_id,
            version_number=next_revision,
            contract_binding_id=contract.catalog_version.contract_binding_id,
            entries=next_entries,
        ),
        source_registry_version_id=None,
        transform_sha256=None,
        created_at=now,
    )
    activation = models.SectionCatalogActivation(
        catalog_activation_id=next_catalog_activation_id,
        generation_id=generation.generation_id,
        catalog_version_id=next_catalog_version_id,
        activation_route="command_execution",
        import_run_id=None,
        command_execution_id=execution.execution_id,
        catalog_revision=next_revision,
        activated_at=now,
    )
    try:
        successor = CatalogRepository(session).install_catalog_revision(
            version=version,
            entries=next_entries,
            activation=activation,
            expected_catalog_version_id=contract.catalog_version.catalog_version_id,
            expected_catalog_activation_id=contract.catalog_activation.catalog_activation_id,
            expected_catalog_revision=contract.active_catalog.catalog_revision,
        )
    except CoreAuthorityError as exc:
        raise NativeSectionLifecycleError("STALE_CATALOG_VIEW", str(exc)) from exc

    next_attestation_id = uuid_factory()
    next_attestation_revision = current_attestation.attestation_revision + 1
    attestation = models.NativeCatalogRuntimeAttestation(
        attestation_id=next_attestation_id,
        generation_id=generation.generation_id,
        catalog_version_id=next_catalog_version_id,
        catalog_activation_id=next_catalog_activation_id,
        predecessor_attestation_id=current_attestation.attestation_id,
        baseline_migration_event_id=None,
        attestation_revision=next_attestation_revision,
        attestation_sha256=models.compute_attestation_sha256(
            generation_id=generation.generation_id,
            catalog_version_id=next_catalog_version_id,
            catalog_activation_id=next_catalog_activation_id,
            contract_binding_id=contract.catalog_version.contract_binding_id,
            attestation_revision=next_attestation_revision,
            predecessor_attestation_id=current_attestation.attestation_id,
            baseline_migration_event_id=None,
        ),
        recorded_at=now,
    )
    session.add(attestation)
    session.flush()
    changed = session.execute(
        update(models.CurrentNativeCatalogRuntime)
        .where(
            models.CurrentNativeCatalogRuntime.generation_id == generation.generation_id,
            models.CurrentNativeCatalogRuntime.attestation_id == pointer.attestation_id,
            models.CurrentNativeCatalogRuntime.catalog_version_id == pointer.catalog_version_id,
            models.CurrentNativeCatalogRuntime.catalog_activation_id == pointer.catalog_activation_id,
            models.CurrentNativeCatalogRuntime.attestation_revision == pointer.attestation_revision,
        )
        .values(
            attestation_id=next_attestation_id,
            catalog_version_id=next_catalog_version_id,
            catalog_activation_id=next_catalog_activation_id,
            attestation_revision=next_attestation_revision,
            updated_at=now,
        )
    )
    if changed.rowcount != 1:
        raise NativeSectionLifecycleError(
            "STALE_CATALOG_VIEW", "native runtime compare-and-swap lost its race"
        )
    session.flush()

    successor_section_ids = {entry.section_id for entry in successor.entries}
    states_stmt = (
        select(models.DishState)
        .where(models.DishState.generation_id == generation.generation_id)
        .order_by(models.DishState.task_id)
    )
    if session.get_bind().dialect.name == "postgresql":
        states_stmt = states_stmt.with_for_update().execution_options(populate_existing=True)
    states = tuple(session.scalars(states_stmt))
    for state in states:
        if state.catalog_version_id != contract.catalog_version.catalog_version_id:
            raise NativeSectionLifecycleError(
                "STALE_DISH_CATALOG_BINDING",
                "Dish catalog binding moved during Section lifecycle mutation",
                data={"dish_id": str(state.task_id)},
            )
        if state.section_id not in successor_section_ids:
            raise NativeSectionLifecycleError(
                "SECTION_NOT_EMPTY",
                "Section lifecycle mutation would orphan a Dish placement",
                data={"dish_id": str(state.task_id), "section_id": str(state.section_id)},
            )
        session.add(
            models.SectionCatalogRebindReceipt(
                generation_id=state.generation_id,
                task_id=state.task_id,
                predecessor_catalog_version_id=state.catalog_version_id,
                catalog_version_id=next_catalog_version_id,
                section_id=state.section_id,
                command_execution_id=execution.execution_id,
                recorded_at=now,
            )
        )
        session.flush()
        state.catalog_version_id = next_catalog_version_id
        state.updated_at = now
        session.flush()

    if command_name == "retire-section":
        section = session.get(models.Section, section_id)
        if section is None or section.lifecycle != "active":
            raise NativeSectionLifecycleError(
                "SECTION_NOT_FOUND", "Section is no longer active", http_status=404
            )
        section.lifecycle = "retired"
        section.retired_at = now
        session.flush()

    try:
        readback = CatalogRepository(session).active_runtime_catalog_contract(
            generation.generation_id
        )
    except CoreAuthorityError as exc:
        raise NativeSectionLifecycleError(
            "AUTHORITATIVE_READBACK_FAILED", str(exc)
        ) from exc
    if (
        readback is None
        or readback.catalog_version.catalog_version_id != next_catalog_version_id
        or readback.catalog_activation.catalog_activation_id != next_catalog_activation_id
        or session.scalar(
            select(func.count())
            .select_from(models.DishState)
            .where(
                models.DishState.generation_id == generation.generation_id,
                models.DishState.catalog_version_id != next_catalog_version_id,
            )
        )
        != 0
    ):
        raise NativeSectionLifecycleError(
            "AUTHORITATIVE_READBACK_FAILED",
            "native Section lifecycle readback did not match the committed successor",
        )

    return {
        "action": command_name,
        "section_id": str(section_id),
        "display_name": display_name,
        "workflow_role": (
            f"native-section-{section_id}"
            if command_name == "create-section"
            else target_entry.workflow_role if target_entry is not None else None
        ),
        "catalog_version_id": str(next_catalog_version_id),
        "catalog_activation_id": str(next_catalog_activation_id),
        "catalog_revision": next_revision,
        "runtime_attestation_id": str(next_attestation_id),
        "runtime_attestation_revision": next_attestation_revision,
        "rebound_dish_count": len(states),
    }
