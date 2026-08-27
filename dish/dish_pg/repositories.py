"""Stage 2 repositories.

Repositories participate in a caller-owned SQLAlchemy session. They flush to expose
constraint failures but never commit, rollback, or open nested transactions.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf


class CoreAuthorityError(ValueError):
    """A requested Stage 2 authority transition is not structurally legal."""


@dataclass(frozen=True)
class ScalarMutationSource:
    route: str
    occurred_at: datetime
    import_run_id: uuid.UUID | None = None
    command_execution_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        valid = (
            self.route == "import"
            and self.import_run_id is not None
            and self.command_execution_id is None
        ) or (
            self.route == "command_execution"
            and self.import_run_id is None
            and self.command_execution_id is not None
        )
        if not valid:
            raise CoreAuthorityError("scalar mutation source is not exact")


@dataclass(frozen=True)
class ScalarDishMutationResult:
    dish_version: int | None
    changed_domains: frozenset[str]


class ScalarDishMutation:
    """Collect and atomically finalize one logical scalar Dish transition."""

    def __init__(
        self,
        session: Session,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_dish_version: int,
        expected_membership_revision: int,
        source: ScalarMutationSource,
        uuid_factory=uuid.uuid4,
    ) -> None:
        self.session = session
        self.generation_id = generation_id
        self.task_id = task_id
        self.expected_dish_version = expected_dish_version
        self.expected_membership_revision = expected_membership_revision
        self.source = source
        self.uuid_factory = uuid_factory
        self._content: models.ContentVersion | None = None
        self._placement: tuple[uuid.UUID | None, uuid.UUID] | None = None
        self._completion: tuple[bool, str] | None = None
        self._archived_at: datetime | None = None
        self._finalized = False

        self.state = session.scalar(
            select(models.DishState)
            .where(
                models.DishState.generation_id == generation_id,
                models.DishState.task_id == task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        membership = session.scalar(
            select(models.TaskMembershipHead)
            .where(
                models.TaskMembershipHead.generation_id == generation_id,
                models.TaskMembershipHead.task_id == task_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if self.state is None or membership is None:
            raise CoreAuthorityError("Dish scalar or membership authority is missing")
        if self.state.dish_version != expected_dish_version:
            raise CoreAuthorityError("Dish scalar authority is stale")
        if membership.membership_revision != expected_membership_revision:
            raise CoreAuthorityError("Dish membership authority is stale")

    @property
    def resulting_dish_version(self) -> int:
        return self.expected_dish_version + 1

    def replace_content(
        self,
        *,
        title: str,
        body: str,
        identity_scheme: str,
        content_identity: str,
        contract_binding_id: uuid.UUID,
        predecessor_content_version_id: uuid.UUID,
        content_version_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        if self._content is not None:
            raise CoreAuthorityError("one scalar mutation may create at most one content occurrence")
        if self.state.current_content_version_id != predecessor_content_version_id:
            raise CoreAuthorityError("content predecessor is not current")
        existing = self.session.scalar(
            select(models.ContentVersion.content_version_id).where(
                models.ContentVersion.generation_id == self.generation_id,
                models.ContentVersion.task_id == self.task_id,
                models.ContentVersion.identity_scheme == identity_scheme,
                models.ContentVersion.content_identity == content_identity,
            )
        )
        if existing is not None:
            raise CoreAuthorityError("content occurrence is not distinct")
        content_version_id = content_version_id or self.uuid_factory()
        self._content = models.ContentVersion(
            content_version_id=content_version_id,
            generation_id=self.generation_id,
            task_id=self.task_id,
            representation_kind="document",
            title=title,
            body=body,
            identity_scheme=identity_scheme,
            content_identity=content_identity,
            creator_route=self.source.route,
            import_run_id=self.source.import_run_id,
            command_execution_id=self.source.command_execution_id,
            predecessor_content_version_id=predecessor_content_version_id,
            contract_binding_id=contract_binding_id,
            created_dish_version=self.resulting_dish_version,
            created_at=self.source.occurred_at,
        )
        return content_version_id

    def place(self, *, section_id: uuid.UUID | None, registry_version_id: uuid.UUID) -> None:
        if self._placement is not None:
            raise CoreAuthorityError("placement may be staged only once")
        if section_id is not None and self.session.get(
            models.SectionRegistryEntry, (registry_version_id, section_id)
        ) is None:
            raise CoreAuthorityError("placement is not present in the selected registry")
        self._placement = (section_id, registry_version_id)

    def set_completion(self, *, completed: bool, reason: str) -> None:
        if self._completion is not None:
            raise CoreAuthorityError("completion may be staged only once")
        if self.source.route == "import" and reason != "imported":
            raise CoreAuthorityError("import completion occurrence must be imported")
        if self.source.route == "command_execution" and reason not in {
            "cooked",
            "archive",
            "reopen_planning",
        }:
            raise CoreAuthorityError("command completion reason is invalid")
        self._completion = (completed, reason)

    def archive(self) -> None:
        if self._archived_at is not None or self.state.archived_at is not None:
            raise CoreAuthorityError("Dish is already archived")
        self._archived_at = self.source.occurred_at

    def finalize(self) -> ScalarDishMutationResult:
        if self._finalized:
            raise CoreAuthorityError("scalar mutation was already finalized")
        self._finalized = True
        changed = frozenset(
            domain
            for domain, staged in (
                ("content", self._content),
                ("placement", self._placement),
                ("completion", self._completion if self._completion is not None else self._archived_at),
            )
            if staged is not None
        )
        if not changed:
            return ScalarDishMutationResult(None, changed)

        next_version = self.resulting_dish_version
        receipt = models.DishMutationReceipt(
            generation_id=self.generation_id,
            task_id=self.task_id,
            dish_version=next_version,
            source_route=self.source.route,
            import_run_id=self.source.import_run_id,
            command_execution_id=self.source.command_execution_id,
            content_changed="content" in changed,
            placement_changed="placement" in changed,
            completion_changed="completion" in changed,
            occurred_at=self.source.occurred_at,
        )
        self.session.add(receipt)
        self.session.flush()
        if self._content is not None:
            self.session.add(self._content)
            self.session.flush()

        values: dict[str, object] = {
            "dish_version": next_version,
            "updated_at": self.source.occurred_at,
        }
        if self._content is not None:
            values["current_content_version_id"] = self._content.content_version_id
        if self._placement is not None:
            values.update(
                section_id=self._placement[0],
                registry_version_id=self._placement[1],
                placement_version=next_version,
            )
        if self._completion is not None:
            values.update(
                completed=self._completion[0],
                completion_reason=self._completion[1],
                completion_version=next_version,
            )
        elif self._archived_at is not None:
            values.update(archived_at=self._archived_at, completion_version=next_version)
        result = self.session.execute(
            update(models.DishState)
            .where(
                models.DishState.generation_id == self.generation_id,
                models.DishState.task_id == self.task_id,
                models.DishState.dish_version == self.expected_dish_version,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CoreAuthorityError("Dish scalar CAS lost to a concurrent writer")
        self.session.flush()
        self.session.expire(self.state)
        return ScalarDishMutationResult(next_version, changed)


class DishRepository:
    def __init__(self, session: Session, *, uuid_factory=uuid.uuid4) -> None:
        self.session = session
        self.uuid_factory = uuid_factory

    def begin_scalar_mutation(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_dish_version: int,
        expected_membership_revision: int,
        source: ScalarMutationSource,
    ) -> ScalarDishMutation:
        return ScalarDishMutation(
            self.session,
            generation_id=generation_id,
            task_id=task_id,
            expected_dish_version=expected_dish_version,
            expected_membership_revision=expected_membership_revision,
            source=source,
            uuid_factory=self.uuid_factory,
        )

    @contextmanager
    def mutate_scalar(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        expected_dish_version: int,
        expected_membership_revision: int,
        source: ScalarMutationSource,
    ) -> Iterator[ScalarDishMutation]:
        mutation = self.begin_scalar_mutation(
            generation_id=generation_id,
            task_id=task_id,
            expected_dish_version=expected_dish_version,
            expected_membership_revision=expected_membership_revision,
            source=source,
        )
        yield mutation


REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE = "registry-role-correction-v1"
REGISTRY_ROLE_CORRECTION_KIND = "registry_role_assignment"
REGISTRY_ROLE_CORRECTION_COMMAND = "revise-section-registry"
_REGISTRY_ROLE_CORRECTION_PROVENANCE_KEYS = {
    "correction_kind",
    "correction_bundle_sha256",
    "source_import_run_id",
    "predecessor_registry_version_id",
    "command_execution_id",
    "requested_roles",
    "result_registry_sha256",
    "source_record_count",
}


def _provenance_uuid(provenance: dict[str, object], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(provenance[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CoreAuthorityError(
            f"registry correction provenance has invalid {key}"
        ) from exc


def _registry_role_correction_provenance(
    session: Session,
    version: models.SectionRegistryVersion,
    import_run: models.ImportRun,
    *,
    allow_claimed_execution: bool = False,
) -> tuple[dict[str, object], uuid.UUID, uuid.UUID]:
    provenance = (
        import_run.provenance if isinstance(import_run.provenance, dict) else {}
    )
    if set(provenance) != _REGISTRY_ROLE_CORRECTION_PROVENANCE_KEYS:
        raise CoreAuthorityError(
            "registry correction ImportRun provenance is not exact"
        )
    if provenance.get("correction_kind") != REGISTRY_ROLE_CORRECTION_KIND:
        raise CoreAuthorityError("registry correction ImportRun is not honestly labeled")
    if type(provenance.get("source_record_count")) is not int or provenance.get(
        "source_record_count"
    ) != 0:
        raise CoreAuthorityError("registry correction cannot claim imported source records")

    predecessor_id = _provenance_uuid(
        provenance, "predecessor_registry_version_id"
    )
    source_import_run_id = _provenance_uuid(provenance, "source_import_run_id")
    command_execution_id = _provenance_uuid(provenance, "command_execution_id")
    requested = provenance.get("requested_roles")
    if not isinstance(requested, dict) or set(requested) != {
        "research_queue",
        "verification_queue",
    }:
        raise CoreAuthorityError(
            "registry role correction requires exact special-role targets"
        )
    targets = {
        role: _provenance_uuid(requested, role)
        for role in ("research_queue", "verification_queue")
    }
    if targets["research_queue"] == targets["verification_queue"]:
        raise CoreAuthorityError("registry role correction targets must be distinct")

    result_registry_sha256 = provenance.get("result_registry_sha256")
    if result_registry_sha256 != version.registry_sha256:
        raise CoreAuthorityError("registry correction result identity is inconsistent")
    expected_high_water_mark = (
        "registry-role-correction:"
        f"{predecessor_id}:{version.registry_sha256}"
    )
    if import_run.baseline_high_water_mark != expected_high_water_mark:
        raise CoreAuthorityError(
            "registry correction high-water identity is inconsistent"
        )

    correction_payload = {
        "format": "dish-registry-role-correction-v1",
        "generation_id": str(version.generation_id),
        "predecessor_registry_version_id": str(predecessor_id),
        "source_import_run_id": str(source_import_run_id),
        "command_execution_id": str(command_execution_id),
        "requested_roles": {
            role: str(section_id) for role, section_id in targets.items()
        },
        "result_registry_sha256": version.registry_sha256,
    }
    expected_bundle_sha256 = hashlib.sha256(
        json.dumps(
            correction_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        provenance.get("correction_bundle_sha256") != expected_bundle_sha256
        or import_run.source_bundle_sha256 != expected_bundle_sha256
    ):
        raise CoreAuthorityError("registry correction bundle identity is inconsistent")

    execution = session.get(wf.CommandExecution, command_execution_id)
    execution_status_ok = execution is not None and (
        execution.status == "committed"
        or (allow_claimed_execution and execution.status == "claimed")
    )
    if (
        not execution_status_ok
        or execution is None
        or execution.generation_id != version.generation_id
        or execution.contract_binding_id != version.contract_binding_id
        or execution.command_name != REGISTRY_ROLE_CORRECTION_COMMAND
    ):
        raise CoreAuthorityError(
            "registry correction command execution provenance is inconsistent"
        )
    request = session.get(wf.ServiceRequest, execution.request_id)
    request_payload = (
        request.canonical_payload
        if request is not None and isinstance(request.canonical_payload, dict)
        else {}
    )
    request_arguments = request_payload.get("arguments")
    if (
        request is None
        or request.generation_id != version.generation_id
        or request.principal_class != "admin"
        or request.command_name != REGISTRY_ROLE_CORRECTION_COMMAND
        or request_payload.get("command") != REGISTRY_ROLE_CORRECTION_COMMAND
        or not isinstance(request_arguments, dict)
    ):
        raise CoreAuthorityError(
            "registry correction admin request provenance is inconsistent"
        )
    requested_argument_names = {
        "research_queue": "research_queue_section_id",
        "verification_queue": "verification_queue_section_id",
    }
    for role, argument_name in requested_argument_names.items():
        try:
            request_target = uuid.UUID(str(request_arguments[argument_name]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CoreAuthorityError(
                "registry correction admin request provenance is inconsistent"
            ) from exc
        if request_target != targets[role]:
            raise CoreAuthorityError(
                "registry correction admin request provenance is inconsistent"
            )

    return provenance, predecessor_id, source_import_run_id


def _registry_source_import_run(
    session: Session,
    version: models.SectionRegistryVersion,
    *,
    allow_current_claimed_execution: bool,
) -> models.ImportRun:
    seen: set[uuid.UUID] = set()
    corrections: list[tuple[models.ImportRun, uuid.UUID]] = []
    current = version
    first_version = True
    while True:
        if current.registry_version_id in seen:
            raise CoreAuthorityError("registry correction provenance contains a cycle")
        seen.add(current.registry_version_id)
        import_run = session.get(models.ImportRun, current.import_run_id)
        if import_run is None or import_run.status != "complete":
            raise CoreAuthorityError("registry version requires a complete import run")
        if import_run.source_release != REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE:
            for correction_run, claimed_source_id in corrections:
                if (
                    claimed_source_id != import_run.import_run_id
                    or correction_run.source_commit != import_run.source_commit
                    or correction_run.legacy_generation_id != import_run.legacy_generation_id
                ):
                    raise CoreAuthorityError(
                        "registry correction source import lineage is inconsistent"
                    )
            return import_run

        provenance, predecessor_id, source_import_run_id = (
            _registry_role_correction_provenance(
                session,
                current,
                import_run,
                allow_claimed_execution=(
                    allow_current_claimed_execution and first_version
                ),
            )
        )
        predecessor = session.get(models.SectionRegistryVersion, predecessor_id)
        if (
            predecessor is None
            or predecessor.generation_id != current.generation_id
            or predecessor.contract_binding_id != current.contract_binding_id
            or current.version_number != predecessor.version_number + 1
        ):
            raise CoreAuthorityError("registry correction predecessor is inconsistent")
        corrections.append((import_run, source_import_run_id))
        current = predecessor
        first_version = False


def registry_source_import_run(
    session: Session,
    version: models.SectionRegistryVersion,
) -> models.ImportRun:
    """Resolve the original source import behind durable registry corrections."""

    return _registry_source_import_run(
        session,
        version,
        allow_current_claimed_execution=False,
    )


def _assert_registry_role_correction_entries(
    session: Session,
    row: models.SectionRegistryVersion,
    entries: tuple[models.SectionRegistryEntry, ...],
    *,
    allow_claimed_execution: bool = False,
) -> None:
    import_run = session.get(models.ImportRun, row.import_run_id)
    assert import_run is not None
    provenance, predecessor_id, _source_import_run_id = (
        _registry_role_correction_provenance(
            session,
            row,
            import_run,
            allow_claimed_execution=allow_claimed_execution,
        )
    )
    requested = provenance["requested_roles"]
    assert isinstance(requested, dict)
    targets = {
        role: _provenance_uuid(requested, role)
        for role in ("research_queue", "verification_queue")
    }
    predecessor = session.get(
        models.SectionRegistryVersion,
        predecessor_id,
    )
    if predecessor is None:
        raise CoreAuthorityError("registry role correction predecessor is missing")
    prior_entries = tuple(
        session.scalars(
            select(models.SectionRegistryEntry).where(
                models.SectionRegistryEntry.registry_version_id
                == predecessor.registry_version_id
            )
        )
    )
    prior_by_section = {entry.section_id: entry for entry in prior_entries}
    if not set(targets.values()).issubset(prior_by_section):
        raise CoreAuthorityError(
            "registry role correction targets must already be registered"
        )
    revised_by_section = {entry.section_id: entry for entry in entries}
    if set(prior_by_section) != set(revised_by_section):
        raise CoreAuthorityError("registry role correction cannot change section membership")
    for section_id, prior in prior_by_section.items():
        revised = revised_by_section[section_id]
        expected_role = (
            "research_queue"
            if section_id == targets["research_queue"]
            else "verification_queue"
            if section_id == targets["verification_queue"]
            else prior.workflow_role
        )
        if (
            revised.ordinal != prior.ordinal
            or revised.display_name != prior.display_name
            or revised.workflow_role != expected_role
        ):
            raise CoreAuthorityError(
                "registry role correction may change only the two special workflow roles"
            )


class AuthorityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_import_run(self, row: models.ImportRun) -> None:
        self.session.add(row)
        self.session.flush()

    def add_generation(self, row: models.AuthorityGeneration) -> None:
        self.session.add(row)
        self.session.flush()

    def generation(self, generation_id: uuid.UUID) -> models.AuthorityGeneration:
        row = self.session.get(models.AuthorityGeneration, generation_id)
        if row is None:
            raise CoreAuthorityError(f"unknown authority generation: {generation_id}")
        return row

    def active_generation(self) -> models.AuthorityGeneration | None:
        return self.session.scalar(
            select(models.AuthorityGeneration).where(models.AuthorityGeneration.status == "active")
        )

    def activate_generation(
        self,
        *,
        generation_id: uuid.UUID,
        activation: models.AuthorityActivation,
        at: datetime,
    ) -> None:
        generation = self.generation(generation_id)
        if generation.status != "pending":
            raise CoreAuthorityError("only a pending generation may be activated")
        current = self.active_generation()
        if current is not None:
            if generation.predecessor_generation_id != current.generation_id:
                raise CoreAuthorityError("restore generation must name the current predecessor")
            current.status = "retired"
            current.retired_at = at
            self.session.flush()
        elif generation.creation_reason != "initial_cutover":
            raise CoreAuthorityError("the first active generation must be initial_cutover")
        generation.status = "active"
        self.session.add(activation)
        self.session.flush()

    def add_bootstrap_authority(self, row: models.GenerationBootstrapAuthority) -> None:
        self.session.add(row)
        self.session.flush()

    def add_migration_event(self, row: models.AppliedMigrationEvent) -> None:
        self.session.add(row)
        self.session.flush()


class ContractBindingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, row: models.HonestContractBinding) -> None:
        self.session.add(row)
        self.session.flush()

    def require(self, binding_id: uuid.UUID) -> models.HonestContractBinding:
        row = self.session.get(models.HonestContractBinding, binding_id)
        if row is None:
            raise CoreAuthorityError(f"unknown Honest contract binding: {binding_id}")
        return row




@dataclass(frozen=True)
class ActiveReleaseContract:
    generation: models.AuthorityGeneration
    active_registry: models.ActiveSectionRegistry
    registry_version: models.SectionRegistryVersion
    honest_binding: models.HonestContractBinding


class RegistryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_project(self, row: models.GovernedProject) -> None:
        self.session.add(row)
        self.session.flush()

    def add_section(self, row: models.GovernedSection) -> None:
        project = self.session.get(models.GovernedProject, row.project_id)
        if project is None or project.lifecycle != "active":
            raise CoreAuthorityError("section requires an active governed project")
        self.session.add(row)
        self.session.flush()

    def add_project_alias(self, row: models.ProjectExternalAlias) -> None:
        self.session.add(row)
        self.session.flush()

    def add_section_alias(self, row: models.SectionExternalAlias) -> None:
        self.session.add(row)
        self.session.flush()

    def add_registry_version(
        self,
        row: models.SectionRegistryVersion,
        entries: Iterable[models.SectionRegistryEntry],
    ) -> None:
        entries = tuple(entries)
        generation = self.session.get(models.AuthorityGeneration, row.generation_id)
        binding = self.session.get(models.HonestContractBinding, row.contract_binding_id)
        import_run = self.session.get(models.ImportRun, row.import_run_id)
        if generation is None or generation.status == "retired":
            raise CoreAuthorityError(
                "registry version requires a non-retired authority generation"
            )
        if binding is None or binding.dish_release != generation.dish_release:
            raise CoreAuthorityError(
                "registry version requires a matching Honest contract binding"
            )
        if import_run is None or import_run.status != "complete":
            raise CoreAuthorityError("registry version requires a complete import run")
        if import_run.source_release == REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE:
            _registry_source_import_run(
                self.session,
                row,
                allow_current_claimed_execution=True,
            )
            _assert_registry_role_correction_entries(
                self.session,
                row,
                entries,
                allow_claimed_execution=True,
            )
        self.session.add(row)
        self.session.flush()
        seen_roles: set[str] = set()
        seen_sections: set[uuid.UUID] = set()
        for entry in entries:
            if entry.registry_version_id != row.registry_version_id:
                raise CoreAuthorityError("registry entry belongs to a different version")
            if entry.workflow_role in seen_roles or entry.section_id in seen_sections:
                raise CoreAuthorityError("registry entries must have unique roles and sections")
            section = self.session.get(models.GovernedSection, entry.section_id)
            if section is None or section.lifecycle != "active":
                raise CoreAuthorityError("registry entry requires an active governed section")
            seen_roles.add(entry.workflow_role)
            seen_sections.add(entry.section_id)
            self.session.add(entry)
        if not seen_sections:
            raise CoreAuthorityError("registry version must contain at least one section")
        self.session.flush()

    def activate_registry(
        self,
        *,
        activation: models.SectionRegistryActivation,
        current: models.ActiveSectionRegistry,
    ) -> None:
        version = self.session.get(
            models.SectionRegistryVersion, activation.registry_version_id
        )
        if version is None or version.generation_id != activation.generation_id:
            raise CoreAuthorityError("registry activation generation/version mismatch")
        if current.generation_id != activation.generation_id:
            raise CoreAuthorityError("active registry generation mismatch")
        if current.registry_version_id != activation.registry_version_id:
            raise CoreAuthorityError("active registry version mismatch")
        if current.registry_activation_id != activation.registry_activation_id:
            raise CoreAuthorityError("active registry activation mismatch")
        if current.registry_revision != activation.registry_revision:
            raise CoreAuthorityError("active registry revision mismatch")
        existing = self.session.get(models.ActiveSectionRegistry, current.generation_id)
        self.session.add(activation)
        self.session.flush()
        if existing is None:
            self.session.add(current)
        else:
            existing.registry_version_id = current.registry_version_id
            existing.registry_activation_id = current.registry_activation_id
            existing.registry_revision = current.registry_revision
            existing.updated_at = current.updated_at
        self.session.flush()

    def active_registry(self, generation_id: uuid.UUID) -> models.ActiveSectionRegistry:
        row = self.session.get(models.ActiveSectionRegistry, generation_id)
        if row is None:
            raise CoreAuthorityError("generation has no active section registry")
        return row

    def active_release_contract(self, generation_id: uuid.UUID) -> ActiveReleaseContract:
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if generation is None or generation.status != "active":
            raise CoreAuthorityError("release contract requires the active authority generation")
        active = self.active_registry(generation_id)
        registry = self.session.get(models.SectionRegistryVersion, active.registry_version_id)
        if registry is None or registry.generation_id != generation_id:
            raise CoreAuthorityError("active registry version does not belong to the active generation")
        binding = self.session.get(models.HonestContractBinding, registry.contract_binding_id)
        if (
            binding is None
            or binding.binding_kind != "release"
            or binding.dish_release != generation.dish_release
        ):
            raise CoreAuthorityError("active registry does not resolve its exact Honest release binding")
        return ActiveReleaseContract(
            generation=generation,
            active_registry=active,
            registry_version=registry,
            honest_binding=binding,
        )

    def require_registered_section(
        self,
        *,
        generation_id: uuid.UUID,
        section_id: uuid.UUID,
    ) -> tuple[models.ActiveSectionRegistry, models.GovernedSection]:
        active = self.active_registry(generation_id)
        entry = self.session.get(
            models.SectionRegistryEntry,
            (active.registry_version_id, section_id),
        )
        if entry is None:
            raise CoreAuthorityError("section is not present in the active registry")
        section = self.session.get(models.GovernedSection, section_id)
        if section is None or section.lifecycle != "active":
            raise CoreAuthorityError("active registry points to an inactive section")
        return active, section


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_imported_task_bundle(
        self,
        *,
        task: models.DishTask,
        alias: models.TaskExternalAlias,
        receipt: models.DishMutationReceipt,
        version: models.ContentVersion,
        state: models.DishState,
        membership_head: models.TaskMembershipHead,
        membership_events: Iterable[models.TaskProjectMembershipEvent],
        current_memberships: Iterable[models.CurrentTaskProjectMembership],
    ) -> None:
        if task.creation_route != "import":
            raise CoreAuthorityError("Stage 2 import bundle requires import creation provenance")
        if alias.task_id != task.task_id or alias.origin != "imported":
            raise CoreAuthorityError("task alias does not match imported task")
        if version.task_id != task.task_id or receipt.task_id != task.task_id:
            raise CoreAuthorityError("content authority does not match imported task")
        if state.current_content_version_id != version.content_version_id:
            raise CoreAuthorityError("DishState does not target imported content")
        if state.task_id != task.task_id or state.generation_id != version.generation_id:
            raise CoreAuthorityError("DishState generation/task mismatch")
        if (
            receipt.generation_id != state.generation_id
            or receipt.dish_version != 1
            or version.created_dish_version != 1
            or state.dish_version != 1
            or state.placement_version != 1
            or state.completion_version != 1
        ):
            raise CoreAuthorityError("initial imported scalar authority must be occurrence one")
        if membership_head.task_id != task.task_id or membership_head.generation_id != state.generation_id:
            raise CoreAuthorityError("membership head generation/task mismatch")

        self.session.add(task)
        self.session.flush()
        self.session.add_all([alias, receipt])
        self.session.flush()
        self.session.add(version)
        self.session.flush()
        self.session.add_all([state, membership_head])
        self.session.flush()

        membership_rows = list(membership_events)
        current_rows = list(current_memberships)
        if len(membership_rows) != len(current_rows):
            raise CoreAuthorityError("each imported membership needs one current row")
        self.session.add_all(membership_rows)
        self.session.flush()
        self.session.add_all(current_rows)
        self.session.flush()
