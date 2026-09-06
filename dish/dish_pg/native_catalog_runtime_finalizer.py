"""Establish the revision-1 native Section runtime root in one caller transaction."""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .native_section_carry_forward import (
    CARRY_FORWARD_PREDECESSOR,
    CARRY_FORWARD_REVISION,
)
from .native_section_content_materializer import (
    NativeSectionContentMaterializationError,
    NativeSectionContentMaterializationResult,
    materialize_staged_native_section_content,
)
from .recovery_control import migration_revision_sha256
from .repositories import CatalogRepository, CoreAuthorityError

FINALIZER_REVISION = "0050_native_catalog_runtime_authority_switch"
FINALIZER_PREDECESSOR = "0049_native_catalog_runtime_authority_root"
AUTHORITY_TRANSITION = "native_section_runtime_root_v1"
REPOSITORY = "marcogallotta/ai-tools"
SOURCE_TASK_GID = "1218149197310340"
EXECUTION_TASK_GID = "1218208389911483"
_INITIATOR = "dish-pg-native-catalog-runtime-finalizer"
_NAMESPACE = uuid.UUID("565c7696-b0cc-49db-95c4-6fe31b66ae12")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_INVENTORY_COUNTS = (
    "total_documents",
    "ready_documents",
    "pending_verification_documents",
    "imported_unsigned_ready_documents",
    "verification_signoffs",
    "legacy_destination_documents",
    "ready_legacy_destination_documents",
    "pending_verification_legacy_destination_documents",
    "ready_without_legacy_destination",
)


class NativeCatalogRuntimeFinalizerError(ValueError):
    """The exact reviewed native runtime switch preconditions are not satisfied."""


@dataclass(frozen=True)
class NativeCatalogRuntimeFinalizerResult:
    generation_id: uuid.UUID
    migration_event_id: uuid.UUID
    attestation_id: uuid.UUID
    catalog_version_id: uuid.UUID
    catalog_activation_id: uuid.UUID
    materialization: NativeSectionContentMaterializationResult
    inserted: bool


def _deterministic_id(generation_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{generation_id}:{FINALIZER_REVISION}:{kind}")


def _git_text(*args: str) -> str:
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer requires a readable Git checkout"
        ) from exc
    return completed.stdout.strip()


def verified_source_commit() -> str:
    """Return exact executable HEAD only from a clean repository checkout."""

    root = Path(__file__).resolve().parents[2].resolve()
    try:
        top = Path(_git_text("rev-parse", "--show-toplevel")).resolve()
    except OSError as exc:  # pragma: no cover - defensive Path boundary
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer cannot resolve repository root"
        ) from exc
    if top != root:
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer is not rooted in the executing Git checkout"
        )
    if _git_text(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer refuses a dirty Git checkout"
        )
    commit = _git_text("rev-parse", "HEAD")
    if not _SHA_RE.fullmatch(commit):
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer requires an exact 40-character source commit"
        )
    return commit


def _lock_active_generation(session: Session) -> models.AuthorityGeneration:
    statement = (
        select(models.AuthorityGeneration)
        .where(models.AuthorityGeneration.status == "active")
        .order_by(models.AuthorityGeneration.generation_id)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    generations = tuple(session.scalars(statement.execution_options(populate_existing=True)))
    if len(generations) != 1:
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer requires exactly one active authority generation"
        )
    return generations[0]


def _lock_active_catalog(
    session: Session, generation_id: uuid.UUID
) -> models.ActiveSectionCatalog:
    statement = select(models.ActiveSectionCatalog).where(
        models.ActiveSectionCatalog.generation_id == generation_id
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    active = session.scalar(statement.execution_options(populate_existing=True))
    if active is None:
        raise NativeCatalogRuntimeFinalizerError(
            "active generation has no native Section catalog"
        )
    return active


def _lock_current_pointer(
    session: Session, generation_id: uuid.UUID
) -> models.CurrentNativeCatalogRuntime | None:
    statement = select(models.CurrentNativeCatalogRuntime).where(
        models.CurrentNativeCatalogRuntime.generation_id == generation_id
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement.execution_options(populate_existing=True))


def _require_inventory_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise NativeCatalogRuntimeFinalizerError(
            "0048 inventory evidence is missing its reviewed counts"
        )
    counts: dict[str, int] = {}
    for key in _REQUIRED_INVENTORY_COUNTS:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise NativeCatalogRuntimeFinalizerError(
                f"0048 inventory count {key!r} is missing or invalid"
            )
        counts[key] = item
    return counts


def _require_0048_event(
    session: Session,
    *,
    generation_id: uuid.UUID,
    catalog: models.ActiveSectionCatalog,
    contract_binding_id: uuid.UUID,
) -> tuple[models.AppliedMigrationEvent, dict[str, int]]:
    event = session.scalar(
        select(models.AppliedMigrationEvent).where(
            models.AppliedMigrationEvent.generation_id == generation_id,
            models.AppliedMigrationEvent.revision == CARRY_FORWARD_REVISION,
            models.AppliedMigrationEvent.outcome == "applied",
        )
    )
    if event is None:
        raise NativeCatalogRuntimeFinalizerError(
            "required same-generation applied 0048 migration event is missing"
        )
    details = event.details if isinstance(event.details, dict) else {}
    counts = _require_inventory_counts(details.get("inventory_counts"))
    staged_count = details.get("staged_occurrence_count")
    if (
        event.predecessor_revision != CARRY_FORWARD_PREDECESSOR
        or event.migration_code_sha256 != migration_revision_sha256(CARRY_FORWARD_REVISION)
        or details.get("authority_transition")
        != "native_section_content_carry_forward_v1"
        or details.get("decision") != "carry_forward_completed"
        or details.get("repository") != REPOSITORY
        or details.get("generation_id") != str(generation_id)
        or details.get("target_catalog_version_id") != str(catalog.catalog_version_id)
        or details.get("target_catalog_activation_id")
        != str(catalog.catalog_activation_id)
        or details.get("target_catalog_revision") != catalog.catalog_revision
        or details.get("honest_contract_binding_id") != str(contract_binding_id)
        or details.get("runtime_switched") is not False
        or details.get("current_dish_state_mutated") is not False
        or not isinstance(staged_count, int)
        or staged_count <= 0
        or staged_count != counts["legacy_destination_documents"]
    ):
        raise NativeCatalogRuntimeFinalizerError(
            "0048 migration event does not match the reviewed active-generation inventory/catalog gate"
        )
    return event, counts


def _inventory_gate(
    event: models.AppliedMigrationEvent, counts: Mapping[str, int]
) -> dict[str, Any]:
    details = event.details
    return {
        "decision": "carry_forward_completed",
        "generation_id": str(event.generation_id),
        "counts": dict(counts),
        "prerequisite_migration_event_id": str(event.migration_event_id),
        "prerequisite_migration_revision": event.revision,
        "prerequisite_migration_code_sha256": event.migration_code_sha256,
        "prerequisite_source_commit_sha": details.get("source_commit_sha"),
        "staged_occurrence_count": details.get("staged_occurrence_count"),
    }


def _finalizer_event(
    session: Session, generation_id: uuid.UUID
) -> models.AppliedMigrationEvent | None:
    return session.scalar(
        select(models.AppliedMigrationEvent).where(
            models.AppliedMigrationEvent.generation_id == generation_id,
            models.AppliedMigrationEvent.revision == FINALIZER_REVISION,
            models.AppliedMigrationEvent.outcome == "applied",
        )
    )


def _event_details_match(
    event: models.AppliedMigrationEvent,
    *,
    generation: models.AuthorityGeneration,
    catalog: models.ActiveSectionCatalog,
    contract_binding_id: uuid.UUID,
    source_commit_sha: str,
    inventory_gate: Mapping[str, Any],
    migration_code_sha256: str,
) -> bool:
    details = event.details if isinstance(event.details, dict) else {}
    return bool(
        event.generation_id == generation.generation_id
        and event.predecessor_revision == FINALIZER_PREDECESSOR
        and event.migration_code_sha256 == migration_code_sha256
        and event.dish_release == generation.dish_release
        and details.get("authority_transition") == AUTHORITY_TRANSITION
        and details.get("source_task_gid") == SOURCE_TASK_GID
        and details.get("execution_task_gid") == EXECUTION_TASK_GID
        and details.get("repository") == REPOSITORY
        and details.get("source_commit_sha") == source_commit_sha
        and details.get("catalog_activation_id") == str(catalog.catalog_activation_id)
        and details.get("catalog_version_id") == str(catalog.catalog_version_id)
        and details.get("honest_contract_binding_id") == str(contract_binding_id)
        and details.get("inventory_gate") == dict(inventory_gate)
    )


def _validate_root(
    session: Session,
    *,
    generation: models.AuthorityGeneration,
    catalog: models.ActiveSectionCatalog,
    contract_binding_id: uuid.UUID,
    event: models.AppliedMigrationEvent,
) -> models.NativeCatalogRuntimeAttestation:
    resolved = models.resolve_current_native_catalog_runtime(
        session, generation.generation_id
    )
    if resolved is None:
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer readback is missing the current pointer"
        )
    pointer, attestation = resolved
    source_commit = event.details.get("source_commit_sha")
    expected_hash = models.compute_attestation_sha256(
        generation_id=generation.generation_id,
        catalog_version_id=catalog.catalog_version_id,
        catalog_activation_id=catalog.catalog_activation_id,
        contract_binding_id=contract_binding_id,
        attestation_revision=1,
        predecessor_attestation_id=None,
        baseline_migration_event_id=event.migration_event_id,
        baseline_revision=event.revision,
        baseline_migration_code_sha256=event.migration_code_sha256,
        baseline_dish_release=event.dish_release,
        baseline_source_commit_sha=str(source_commit),
    )
    if (
        pointer.catalog_version_id != catalog.catalog_version_id
        or pointer.catalog_activation_id != catalog.catalog_activation_id
        or pointer.attestation_revision != 1
        or attestation.generation_id != generation.generation_id
        or attestation.catalog_version_id != catalog.catalog_version_id
        or attestation.catalog_activation_id != catalog.catalog_activation_id
        or attestation.predecessor_attestation_id is not None
        or attestation.baseline_migration_event_id != event.migration_event_id
        or attestation.attestation_revision != 1
        or attestation.attestation_sha256 != expected_hash
    ):
        raise NativeCatalogRuntimeFinalizerError(
            "native runtime finalizer readback does not match the exact revision-1 root"
        )
    return attestation


def finalize_native_catalog_runtime_authority(
    session: Session,
    *,
    source_commit_sha: str | None = None,
    now: datetime | None = None,
) -> NativeCatalogRuntimeFinalizerResult:
    """Atomically materialize staged content and establish native runtime authority.

    The caller owns the surrounding transaction. This function flushes but never commits.
    """

    source_commit_sha = source_commit_sha or verified_source_commit()
    if not _SHA_RE.fullmatch(source_commit_sha):
        raise NativeCatalogRuntimeFinalizerError(
            "source commit must be an exact 40-character lowercase Git SHA"
        )
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise NativeCatalogRuntimeFinalizerError("finalizer timestamp must be timezone-aware")

    generation = _lock_active_generation(session)
    locked_catalog = _lock_active_catalog(session, generation.generation_id)
    try:
        contract = CatalogRepository(session).active_catalog_contract(
            generation.generation_id
        )
    except CoreAuthorityError as exc:
        raise NativeCatalogRuntimeFinalizerError(str(exc)) from exc
    if (
        contract.active_catalog.catalog_version_id != locked_catalog.catalog_version_id
        or contract.active_catalog.catalog_activation_id
        != locked_catalog.catalog_activation_id
        or contract.active_catalog.catalog_revision != locked_catalog.catalog_revision
    ):
        raise NativeCatalogRuntimeFinalizerError(
            "active native catalog moved while finalizer held its authority lock"
        )

    carry_event, counts = _require_0048_event(
        session,
        generation_id=generation.generation_id,
        catalog=locked_catalog,
        contract_binding_id=contract.honest_binding.binding_id,
    )
    gate = _inventory_gate(carry_event, counts)
    code_sha = migration_revision_sha256(FINALIZER_REVISION)
    existing_event = _finalizer_event(session, generation.generation_id)
    pointer = _lock_current_pointer(session, generation.generation_id)
    existing_attestations = tuple(
        session.scalars(
            select(models.NativeCatalogRuntimeAttestation).where(
                models.NativeCatalogRuntimeAttestation.generation_id
                == generation.generation_id
            )
        )
    )

    if existing_event is not None or pointer is not None or existing_attestations:
        if existing_event is None or pointer is None:
            raise NativeCatalogRuntimeFinalizerError(
                "partial native runtime authority state exists; finalizer refuses repair"
            )
        if not _event_details_match(
            existing_event,
            generation=generation,
            catalog=locked_catalog,
            contract_binding_id=contract.honest_binding.binding_id,
            source_commit_sha=source_commit_sha,
            inventory_gate=gate,
            migration_code_sha256=code_sha,
        ):
            raise NativeCatalogRuntimeFinalizerError(
                "existing native runtime finalizer event conflicts with this exact run"
            )
        try:
            materialization = materialize_staged_native_section_content(
                session,
                generation_id=generation.generation_id,
                migration_event_id=carry_event.migration_event_id,
                catalog_version_id=locked_catalog.catalog_version_id,
                materialized_at=existing_event.terminal_at,
            )
        except NativeSectionContentMaterializationError as exc:
            raise NativeCatalogRuntimeFinalizerError(str(exc)) from exc
        attestation = _validate_root(
            session,
            generation=generation,
            catalog=locked_catalog,
            contract_binding_id=contract.honest_binding.binding_id,
            event=existing_event,
        )
        return NativeCatalogRuntimeFinalizerResult(
            generation_id=generation.generation_id,
            migration_event_id=existing_event.migration_event_id,
            attestation_id=attestation.attestation_id,
            catalog_version_id=locked_catalog.catalog_version_id,
            catalog_activation_id=locked_catalog.catalog_activation_id,
            materialization=materialization,
            inserted=False,
        )

    event = models.AppliedMigrationEvent(
        migration_event_id=_deterministic_id(generation.generation_id, "migration-event"),
        generation_id=generation.generation_id,
        revision=FINALIZER_REVISION,
        predecessor_revision=FINALIZER_PREDECESSOR,
        migration_code_sha256=code_sha,
        dish_release=generation.dish_release,
        initiator=_INITIATOR,
        outcome="applied",
        started_at=now,
        terminal_at=now,
        details={
            "authority_transition": AUTHORITY_TRANSITION,
            "source_task_gid": SOURCE_TASK_GID,
            "execution_task_gid": EXECUTION_TASK_GID,
            "repository": REPOSITORY,
            "source_commit_sha": source_commit_sha,
            "catalog_activation_id": str(locked_catalog.catalog_activation_id),
            "catalog_version_id": str(locked_catalog.catalog_version_id),
            "honest_contract_binding_id": str(contract.honest_binding.binding_id),
            "inventory_gate": gate,
        },
    )
    session.add(event)
    session.flush()

    try:
        materialization = materialize_staged_native_section_content(
            session,
            generation_id=generation.generation_id,
            migration_event_id=carry_event.migration_event_id,
            catalog_version_id=locked_catalog.catalog_version_id,
            materialized_at=now,
        )
    except NativeSectionContentMaterializationError as exc:
        raise NativeCatalogRuntimeFinalizerError(str(exc)) from exc

    attestation_id = _deterministic_id(generation.generation_id, "attestation-revision-1")
    attestation = models.NativeCatalogRuntimeAttestation(
        attestation_id=attestation_id,
        generation_id=generation.generation_id,
        catalog_version_id=locked_catalog.catalog_version_id,
        catalog_activation_id=locked_catalog.catalog_activation_id,
        predecessor_attestation_id=None,
        baseline_migration_event_id=event.migration_event_id,
        attestation_revision=1,
        attestation_sha256=models.compute_attestation_sha256(
            generation_id=generation.generation_id,
            catalog_version_id=locked_catalog.catalog_version_id,
            catalog_activation_id=locked_catalog.catalog_activation_id,
            contract_binding_id=contract.honest_binding.binding_id,
            attestation_revision=1,
            predecessor_attestation_id=None,
            baseline_migration_event_id=event.migration_event_id,
            baseline_revision=event.revision,
            baseline_migration_code_sha256=event.migration_code_sha256,
            baseline_dish_release=event.dish_release,
            baseline_source_commit_sha=source_commit_sha,
        ),
        recorded_at=now,
    )
    session.add(attestation)
    session.flush()
    session.add(
        models.CurrentNativeCatalogRuntime(
            generation_id=generation.generation_id,
            attestation_id=attestation_id,
            catalog_version_id=locked_catalog.catalog_version_id,
            catalog_activation_id=locked_catalog.catalog_activation_id,
            attestation_revision=1,
            updated_at=now,
        )
    )
    session.flush()

    readback = _validate_root(
        session,
        generation=generation,
        catalog=locked_catalog,
        contract_binding_id=contract.honest_binding.binding_id,
        event=event,
    )
    return NativeCatalogRuntimeFinalizerResult(
        generation_id=generation.generation_id,
        migration_event_id=event.migration_event_id,
        attestation_id=readback.attestation_id,
        catalog_version_id=locked_catalog.catalog_version_id,
        catalog_activation_id=locked_catalog.catalog_activation_id,
        materialization=materialization,
        inserted=True,
    )
