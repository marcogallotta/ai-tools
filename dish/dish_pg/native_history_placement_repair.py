"""One-off, append-only repair of imported Cooking History Section placement."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import models
from .native_section_carry_forward import CARRY_FORWARD_REVISION
from .repositories import CatalogRepository, CoreAuthorityError

REPAIR_REVISION = "0050_native_history_section_placement_repair"
REPAIR_TRANSITION = "native_history_section_placement_repair_v1"
SNAPSHOT_SHA256 = "2f0b812c48c1de4d945043b498af18392b7463c923dd240188b3bc1bf29f854c"
SNAPSHOT_RECORD_COUNT = 265
_SNAPSHOT_PATH = (
    Path(__file__).with_name("data") / "native_history_placement_repair_v1.json"
)
_NAMESPACE = uuid.UUID("75f32440-f787-49e3-815d-cc7ebd2072cf")
_INITIATOR = "dish-pg-native-history-placement-repair"
_NEW_SECTION_NAMES = frozenset(
    {"Indo/Malay/Singapore", "Southeast Asia Misc.", "Mediterranean herbs"}
)

# Immutable 0048 rows whose persisted display label was already stale when staged.
# This is deliberately exact-row evidence, not a general label-equivalence rule.
LEGACY_0048_LABEL_CORRECTIONS = {
    uuid.UUID("4366cc9b-85a4-5404-b882-7647d0c0199d"): (
        uuid.UUID("ab25666a-1320-5d9b-b5c5-1b52d7a8595e"),
        "Indonesia/Malaysia",
        "Indo/Malaysia/Singapore",
    ),
    uuid.UUID("4782666b-169b-5e2f-aece-8a7b189fe63f"): (
        uuid.UUID("ab25666a-1320-5d9b-b5c5-1b52d7a8595e"),
        "Indonesia/Malaysia",
        "Indo/Malaysia/Singapore",
    ),
    uuid.UUID("6bb5065c-57a9-58bc-9624-8b539b52ccb1"): (
        uuid.UUID("ab25666a-1320-5d9b-b5c5-1b52d7a8595e"),
        "Indonesia/Malaysia",
        "Indo/Malaysia/Singapore",
    ),
    uuid.UUID("76231d66-5c41-578b-b440-a2e92985ba98"): (
        uuid.UUID("ab25666a-1320-5d9b-b5c5-1b52d7a8595e"),
        "Indonesia/Malaysia",
        "Indo/Malaysia/Singapore",
    ),
}


class NativeHistoryPlacementRepairError(ValueError):
    """The exact Cooking History recovery contract is not satisfied."""


@dataclass(frozen=True)
class NativeHistoryPlacementRepairPlan:
    generation_id: uuid.UUID
    catalog_version_id: uuid.UUID
    catalog_activation_id: uuid.UUID
    desired_sections: dict[uuid.UUID, uuid.UUID]
    source_section_gids: dict[uuid.UUID, str]
    existing_event: models.AppliedMigrationEvent | None


@dataclass(frozen=True)
class NativeHistoryPlacementRepairResult:
    migration_event_id: uuid.UUID
    repaired_count: int
    already_repaired_count: int
    gate: dict[str, object]


def _deterministic_id(generation_id: uuid.UUID, kind: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{generation_id}:{REPAIR_REVISION}:{kind}")


def _load_snapshot() -> tuple[dict[str, str], tuple[tuple[str, str], ...], str]:
    raw = _SNAPSHOT_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SNAPSHOT_SHA256:
        raise NativeHistoryPlacementRepairError(
            "Cooking History snapshot digest changed"
        )
    payload = json.loads(raw)
    source_sections = payload.get("source_sections")
    sections = payload.get("canonical_sections")
    records = payload.get("records")
    if (
        payload.get("schema") != "dish-native-history-placement-repair-v1"
        or payload.get("source_project_gid") != "1215259129474849"
        or not isinstance(source_sections, dict)
        or not isinstance(sections, dict)
        or not isinstance(records, list)
        or len(records) != SNAPSHOT_RECORD_COUNT
    ):
        raise NativeHistoryPlacementRepairError(
            "Cooking History snapshot shape changed"
        )
    normalized_sections = {str(gid): str(name) for gid, name in sections.items()}
    normalized_source_sections = {
        str(gid): str(name) for gid, name in source_sections.items()
    }
    normalized_records = tuple(
        (str(row.get("task_gid", "")), str(row.get("section_gid", "")))
        for row in records
    )
    if (
        set(normalized_source_sections) != set(normalized_sections)
        or normalized_source_sections["1217202747684673"] != "Indo/Malay/Signapore"
        or normalized_sections["1217202747684673"] != "Indo/Malay/Singapore"
        or any(
            normalized_source_sections[gid] != name
            for gid, name in normalized_sections.items()
            if gid != "1217202747684673"
        )
        or len({task_gid for task_gid, _ in normalized_records})
        != SNAPSHOT_RECORD_COUNT
        or any(not task_gid.isdigit() for task_gid, _ in normalized_records)
        or any(
            section_gid not in normalized_sections
            for _, section_gid in normalized_records
        )
        or set(normalized_sections.values()).intersection(_NEW_SECTION_NAMES)
        != _NEW_SECTION_NAMES
    ):
        raise NativeHistoryPlacementRepairError(
            "Cooking History snapshot identities changed"
        )
    return normalized_sections, normalized_records, digest


def _repair_event(
    session: Session, generation_id: uuid.UUID
) -> models.AppliedMigrationEvent | None:
    return session.scalar(
        select(models.AppliedMigrationEvent).where(
            models.AppliedMigrationEvent.generation_id == generation_id,
            models.AppliedMigrationEvent.revision == REPAIR_REVISION,
            models.AppliedMigrationEvent.outcome == "repair",
        )
    )


def _catalog_hash(entries: list[models.SectionCatalogEntry]) -> str:
    payload = [
        {
            "display_name": row.display_name,
            "ordinal": row.ordinal,
            "section_id": str(row.section_id),
            "workflow_role": row.workflow_role,
        }
        for row in sorted(entries, key=lambda item: item.ordinal)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _install_repair_catalog(
    session: Session,
    *,
    generation: models.AuthorityGeneration,
    current: models.ActiveSectionCatalog,
    contract_binding_id: uuid.UUID,
    section_names: dict[str, str],
    now: datetime,
) -> models.ActiveSectionCatalog:
    current_entries = list(
        session.scalars(
            select(models.SectionCatalogEntry)
            .where(
                models.SectionCatalogEntry.catalog_version_id
                == current.catalog_version_id
            )
            .order_by(models.SectionCatalogEntry.ordinal)
        )
    )
    by_name = {row.display_name: row for row in current_entries}
    if len(by_name) != len(current_entries):
        raise NativeHistoryPlacementRepairError(
            "active catalog has duplicate display names"
        )
    expected_existing = set(section_names.values()) - _NEW_SECTION_NAMES
    if not expected_existing.issubset(by_name):
        raise NativeHistoryPlacementRepairError(
            "active catalog cannot represent the exact Cooking History snapshot"
        )
    present_new = _NEW_SECTION_NAMES.intersection(by_name)
    if present_new:
        if present_new != _NEW_SECTION_NAMES:
            raise NativeHistoryPlacementRepairError(
                "active catalog has a partial Cooking History repair"
            )
        return current

    version_id = _deterministic_id(generation.generation_id, "catalog-version")
    activation_id = _deterministic_id(generation.generation_id, "catalog-activation")
    if session.get(models.SectionCatalogVersion, version_id) is not None:
        raise NativeHistoryPlacementRepairError(
            "deterministic repair catalog identity is occupied"
        )
    entries = [
        models.SectionCatalogEntry(
            catalog_version_id=version_id,
            section_id=row.section_id,
            ordinal=row.ordinal,
            display_name=row.display_name,
            workflow_role=row.workflow_role,
        )
        for row in current_entries
    ]
    next_ordinal = max(row.ordinal for row in current_entries) + 1
    for section_gid, display_name in sorted(section_names.items()):
        if display_name not in _NEW_SECTION_NAMES:
            continue
        section_id = stable_dish_uuid_for_asana_identity("section", section_gid)
        existing = session.get(models.Section, section_id)
        if existing is None:
            CatalogRepository(session).add_section(
                models.Section(
                    section_id=section_id,
                    logical_name=display_name,
                    lifecycle="active",
                    created_at=now,
                    retired_at=None,
                )
            )
        elif existing.logical_name != display_name or existing.lifecycle != "active":
            raise NativeHistoryPlacementRepairError(
                f"native Section identity for {display_name!r} conflicts"
            )
        entries.append(
            models.SectionCatalogEntry(
                catalog_version_id=version_id,
                section_id=section_id,
                ordinal=next_ordinal,
                display_name=display_name,
                workflow_role=f"history-section-{section_gid}",
            )
        )
        next_ordinal += 1
    version = models.SectionCatalogVersion(
        catalog_version_id=version_id,
        generation_id=generation.generation_id,
        version_number=current.catalog_revision + 1,
        contract_binding_id=contract_binding_id,
        catalog_sha256=_catalog_hash(entries),
        source_registry_version_id=None,
        transform_sha256=None,
        created_at=now,
    )
    activation = models.SectionCatalogActivation(
        catalog_activation_id=activation_id,
        generation_id=generation.generation_id,
        catalog_version_id=version_id,
        activation_route="recovery",
        import_run_id=None,
        command_execution_id=None,
        catalog_revision=current.catalog_revision + 1,
        activated_at=now,
    )
    try:
        installed = CatalogRepository(session).install_catalog_revision(
            version=version,
            entries=entries,
            activation=activation,
            expected_catalog_version_id=current.catalog_version_id,
            expected_catalog_activation_id=current.catalog_activation_id,
            expected_catalog_revision=current.catalog_revision,
        )
    except CoreAuthorityError as exc:
        raise NativeHistoryPlacementRepairError(str(exc)) from exc
    return installed.active_catalog


def prepare_native_history_placement_repair(
    session: Session,
    *,
    generation: models.AuthorityGeneration,
    catalog: models.ActiveSectionCatalog,
    contract_binding_id: uuid.UUID,
    now: datetime,
) -> NativeHistoryPlacementRepairPlan | None:
    """Recognize the exact imported cohort and prepare its additive catalog revision."""

    section_names, records, _digest = _load_snapshot()
    task_gids = tuple(task_gid for task_gid, _ in records)
    alias_rows = tuple(
        session.execute(
            select(
                models.TaskExternalAlias.external_id, models.TaskExternalAlias.task_id
            )
            .where(
                models.TaskExternalAlias.external_system == "asana",
                models.TaskExternalAlias.state == "active",
                models.TaskExternalAlias.external_id.in_(task_gids),
            )
            .order_by(models.TaskExternalAlias.external_id)
        )
    )
    if not alias_rows:
        return None
    if len(alias_rows) != SNAPSHOT_RECORD_COUNT:
        raise NativeHistoryPlacementRepairError(
            "Cooking History recovery cohort is only partially present"
        )
    aliases = {str(gid): task_id for gid, task_id in alias_rows}
    expected_tasks = {
        stable_dish_uuid_for_asana_identity("task", gid) for gid in task_gids
    }
    if set(aliases) != set(task_gids) or set(aliases.values()) != expected_tasks:
        raise NativeHistoryPlacementRepairError(
            "Cooking History aliases do not match stable Dish identities"
        )
    event = _repair_event(session, generation.generation_id)
    null_tasks = set(
        session.scalars(
            select(models.DishState.task_id).where(
                models.DishState.generation_id == generation.generation_id,
                models.DishState.section_id.is_(None),
            )
        )
    )
    if event is None and null_tasks != expected_tasks:
        raise NativeHistoryPlacementRepairError(
            "NULL placement set is not the exact Cooking History snapshot"
        )
    if event is not None and null_tasks:
        raise NativeHistoryPlacementRepairError(
            "recorded Cooking History repair still has NULL placements"
        )

    active = _install_repair_catalog(
        session,
        generation=generation,
        current=catalog,
        contract_binding_id=contract_binding_id,
        section_names=section_names,
        now=now,
    )
    entries = tuple(
        session.scalars(
            select(models.SectionCatalogEntry).where(
                models.SectionCatalogEntry.catalog_version_id
                == active.catalog_version_id
            )
        )
    )
    by_name = {row.display_name: row.section_id for row in entries}
    if not set(section_names.values()).issubset(by_name):
        raise NativeHistoryPlacementRepairError(
            "repair catalog does not contain every exact History Section"
        )
    desired = {
        aliases[task_gid]: by_name[section_names[section_gid]]
        for task_gid, section_gid in records
    }
    source_section_gids = {
        aliases[task_gid]: section_gid for task_gid, section_gid in records
    }
    if event is not None:
        details = event.details if isinstance(event.details, dict) else {}
        if (
            details.get("authority_transition") != REPAIR_TRANSITION
            or details.get("snapshot_sha256") != SNAPSHOT_SHA256
            or details.get("snapshot_record_count") != SNAPSHOT_RECORD_COUNT
            or details.get("catalog_version_id") != str(active.catalog_version_id)
            or details.get("catalog_activation_id") != str(active.catalog_activation_id)
        ):
            raise NativeHistoryPlacementRepairError(
                "existing Cooking History repair event conflicts"
            )
    return NativeHistoryPlacementRepairPlan(
        generation_id=generation.generation_id,
        catalog_version_id=active.catalog_version_id,
        catalog_activation_id=active.catalog_activation_id,
        desired_sections=desired,
        source_section_gids=source_section_gids,
        existing_event=event,
    )


def apply_native_history_placement_repair(
    session: Session,
    *,
    plan: NativeHistoryPlacementRepairPlan,
    generation: models.AuthorityGeneration,
    repaired_at: datetime,
) -> NativeHistoryPlacementRepairResult:
    """Apply the exact placement-only repair without committing its caller transaction."""

    task_ids = tuple(sorted(plan.desired_sections))
    statement = (
        select(models.DishState)
        .where(
            models.DishState.generation_id == plan.generation_id,
            models.DishState.task_id.in_(task_ids),
        )
        .order_by(models.DishState.task_id)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    states = {row.task_id: row for row in session.scalars(statement)}
    if set(states) != set(task_ids):
        raise NativeHistoryPlacementRepairError(
            "one or more Cooking History repair states are missing"
        )

    repaired = 0
    already = 0
    for task_id in task_ids:
        state = states[task_id]
        desired_section_id = plan.desired_sections[task_id]
        if (
            state.section_id == desired_section_id
            and state.catalog_version_id == plan.catalog_version_id
        ):
            already += 1
            continue
        if state.catalog_version_id not in (None, plan.catalog_version_id):
            raise NativeHistoryPlacementRepairError(
                "Cooking History placement moved to another catalog during repair"
            )
        content = session.get(models.ContentVersion, state.current_content_version_id)
        if content is None or content.import_run_id is None:
            raise NativeHistoryPlacementRepairError(
                "Cooking History repair requires imported current content provenance"
            )
        next_version = state.dish_version + 1
        if (
            session.get(
                models.DishMutationReceipt,
                (plan.generation_id, task_id, next_version),
            )
            is not None
        ):
            raise NativeHistoryPlacementRepairError(
                "Cooking History placement mutation slot is occupied"
            )
        session.add(
            models.DishMutationReceipt(
                generation_id=plan.generation_id,
                task_id=task_id,
                dish_version=next_version,
                source_route="import",
                import_run_id=content.import_run_id,
                command_execution_id=None,
                content_changed=False,
                placement_changed=True,
                completion_changed=False,
                archive_changed=False,
                occurred_at=repaired_at,
            )
        )
        session.flush()
        changed = session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == plan.generation_id,
                models.DishState.task_id == task_id,
                models.DishState.dish_version == state.dish_version,
                models.DishState.current_content_version_id
                == state.current_content_version_id,
            )
            .values(
                section_id=desired_section_id,
                catalog_version_id=plan.catalog_version_id,
                dish_version=next_version,
                placement_version=next_version,
                updated_at=repaired_at,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise NativeHistoryPlacementRepairError(
                "Cooking History placement compare-and-swap lost its race"
            )
        session.flush()
        repaired += 1

    event_id = _deterministic_id(plan.generation_id, "migration-event")
    code_sha = hashlib.sha256(
        f"{REPAIR_REVISION}\0{SNAPSHOT_SHA256}\0v1".encode()
    ).hexdigest()
    gate: dict[str, object] = {
        "authority_transition": REPAIR_TRANSITION,
        "snapshot_sha256": SNAPSHOT_SHA256,
        "snapshot_record_count": SNAPSHOT_RECORD_COUNT,
        "catalog_version_id": str(plan.catalog_version_id),
        "catalog_activation_id": str(plan.catalog_activation_id),
        "repaired_count": repaired,
        "already_repaired_count": already,
    }
    if plan.existing_event is None:
        event = models.AppliedMigrationEvent(
            migration_event_id=event_id,
            generation_id=plan.generation_id,
            revision=REPAIR_REVISION,
            predecessor_revision=CARRY_FORWARD_REVISION,
            migration_code_sha256=code_sha,
            dish_release=generation.dish_release,
            initiator=_INITIATOR,
            outcome="repair",
            started_at=repaired_at,
            terminal_at=repaired_at,
            details=dict(gate),
        )
        session.add(event)
        session.flush()
    else:
        event = plan.existing_event
        details = event.details if isinstance(event.details, dict) else {}
        stable_gate = {
            key: details.get(key) for key in gate if not key.endswith("_count")
        }
        expected_stable = {
            key: value for key, value in gate.items() if not key.endswith("_count")
        }
        if (
            event.migration_event_id != event_id
            or event.migration_code_sha256 != code_sha
            or stable_gate != expected_stable
            or already != SNAPSHOT_RECORD_COUNT
        ):
            raise NativeHistoryPlacementRepairError(
                "existing Cooking History repair does not match exact repaired state"
            )
        gate = dict(details)
    remaining = int(
        session.scalar(
            select(func.count())
            .select_from(models.DishState)
            .where(
                models.DishState.generation_id == plan.generation_id,
                models.DishState.section_id.is_(None),
            )
        )
        or 0
    )
    if remaining:
        raise NativeHistoryPlacementRepairError(
            "Cooking History repair left NULL canonical placements"
        )
    return NativeHistoryPlacementRepairResult(
        migration_event_id=event.migration_event_id,
        repaired_count=repaired,
        already_repaired_count=already,
        gate=gate,
    )
