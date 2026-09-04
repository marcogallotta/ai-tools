"""Create the first PostgreSQL authority generation from real source evidence.

This is the deliberately narrow bootstrap path that precedes normal generation
transition authority.  It is valid only for an empty target and writes the same
foundational rows as the PostgreSQL test fixture, using caller-supplied and
checkout-resolved identities rather than fixture placeholders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import Engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from dish_tool.constants import (
    DISH_VERSION_FILENAME,
    PROTOCOL_FILENAMES,
    SUPPORTED_PROTOCOL_VERSION,
    SUPPORTED_TASK_SCHEMA_VERSION,
    TASK_SCHEMA_FILENAME,
)
from dish_tool.identifiers import stable_dish_uuid_for_asana_identity
from dish_tool.releases import parse_dish_version
from dish_tool.schema_validation import validate_task_schema_shape

from . import models
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .importer import operation_history_from_mapping
from .release_history import (
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_SOURCE_CONTRACT,
)
from .repositories import AuthorityRepository, ContractBindingRepository, RegistryRepository

DEFAULT_DISH_COMMIT = "9926e511297af94e3897eb65f02c902cadcc016f"
DEFAULT_HONEST_COMMIT = "f4e16e369c4ea90fe287c13975a35ab0afd985d5"
DEFAULT_SCHEMA_HEAD = "0048_native_section_content_carry_forward"
DEFAULT_DATABASE_NAME = "dish_stage_a_dark_test"
DEFAULT_PROJECT_GID = "1216693403164366"
DEFAULT_PROJECT_ID = uuid.UUID("1ae6e7ba-31e3-5dc5-9565-4ea37b49ac97")


class InitialBootstrapError(ValueError):
    """The requested first-generation bootstrap is unsafe or inconsistent."""


@dataclass(frozen=True)
class SourceBundle:
    path: Path
    sha256: str
    record_count: int
    max_observed_at: datetime
    sections: Mapping[uuid.UUID, str]
    section_names: Mapping[uuid.UUID, str] = field(default_factory=dict)
    explicit_operation_revocations: bool = False

    @property
    def high_water_mark(self) -> str:
        observed = self.max_observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return f"observed-at:{observed};bundle-sha256:{self.sha256}"


@dataclass(frozen=True)
class HonestCheckout:
    root: Path
    commit: str
    protocol_version: str
    schema_version: str
    protocol_sha256: str
    schema_sha256: str
    protocol_files: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class SectionSpec:
    section_id: uuid.UUID
    section_gid: str
    section_name: str
    workflow_role: str


def section_specs_from_bundle(source_bundle: SourceBundle) -> tuple[SectionSpec, ...]:
    """Derive one registrable section per distinct section discovered in the corpus.

    Current source bundles preserve the Asana section display name. Older
    programmatically-constructed bundles may omit it, in which case the
    historical placeholder remains as a compatibility fallback. Workflow role
    is still an explicit bootstrap concern and is never inferred from the name.
    Ordered by gid for a deterministic registry ordinal.
    """
    return tuple(
        SectionSpec(
            section_id=section_id,
            section_gid=section_gid,
            section_name=source_bundle.section_names.get(
                section_id, f"Imported section {section_gid}"
            ),
            workflow_role=f"imported-section-{section_gid}",
        )
        for section_id, section_gid in sorted(
            source_bundle.sections.items(), key=lambda item: item[1]
        )
    )


def ensure_required_section(
    sections: tuple[SectionSpec, ...],
    *,
    section_id: uuid.UUID,
    section_gid: str,
    section_name: str,
) -> tuple[SectionSpec, ...]:
    """Preserve an explicitly configured workflow section even when it is empty."""
    if not section_gid.isdigit() or section_gid.startswith("0"):
        raise InitialBootstrapError(
            "required section GID must be a canonical positive decimal Asana GID"
        )
    expected_section_id = stable_dish_uuid_for_asana_identity("section", section_gid)
    if section_id != expected_section_id:
        raise InitialBootstrapError(
            f"required section_id {section_id} does not match section_gid {section_gid}"
        )
    if not section_name.strip():
        raise InitialBootstrapError("required section name must be nonblank")
    for section in sections:
        if section.section_id == section_id:
            if section.section_gid != section_gid:
                raise InitialBootstrapError(
                    f"required section {section_id} maps to both section_gid "
                    f"{section.section_gid} and {section_gid}"
                )
            return sections
        if section.section_gid == section_gid:
            raise InitialBootstrapError(
                f"required section_gid {section_gid} maps to multiple section IDs"
            )
    return sections + (
        SectionSpec(
            section_id=section_id,
            section_gid=section_gid,
            section_name=section_name.strip(),
            workflow_role=f"imported-section-{section_gid}",
        ),
    )


def apply_research_queue_role(
    sections: tuple[SectionSpec, ...], *, research_queue_section_id: uuid.UUID
) -> tuple[SectionSpec, ...]:
    """Designate one already-discovered section as the Research Queue.

    A real corpus carries no operator role assignment, so nothing about a
    section's content can imply it is the Research Queue; that is always an
    explicit operator/bootstrap-time decision, made here rather than guessed
    from the source bundle.
    """
    if not any(section.section_id == research_queue_section_id for section in sections):
        raise InitialBootstrapError(
            f"--research-queue-section-id {research_queue_section_id} is not a discovered section"
        )
    return tuple(
        section
        if section.section_id != research_queue_section_id
        else SectionSpec(
            section_id=section.section_id,
            section_gid=section.section_gid,
            section_name=section.section_name,
            workflow_role="research_queue",
        )
        for section in sections
    )


def apply_verification_queue_role(
    sections: tuple[SectionSpec, ...], *, verification_queue_section_id: uuid.UUID
) -> tuple[SectionSpec, ...]:
    """Designate one already-discovered section as the Verification Queue."""
    if not any(section.section_id == verification_queue_section_id for section in sections):
        raise InitialBootstrapError(
            f"--verification-queue-section-id {verification_queue_section_id} is not a discovered section"
        )
    return tuple(
        section
        if section.section_id != verification_queue_section_id
        else SectionSpec(
            section_id=section.section_id,
            section_gid=section.section_gid,
            section_name=section.section_name,
            workflow_role="verification_queue",
        )
        for section in sections
    )


@dataclass(frozen=True)
class InitialBootstrapSpec:
    dish_commit: str
    schema_head: str
    source_generation: str
    source_bundle: SourceBundle
    honest: HonestCheckout
    project_id: uuid.UUID
    project_gid: str
    project_name: str
    sections: tuple[SectionSpec, ...]

    @property
    def dish_release(self) -> str:
        return f"dish@{self.dish_commit}"

    @property
    def honest_release(self) -> str:
        return f"honest-pantry@{self.honest.commit}"


@dataclass(frozen=True)
class SectionResult:
    section_id: uuid.UUID
    section_gid: str
    section_alias_id: uuid.UUID


@dataclass(frozen=True)
class InitialBootstrapResult:
    import_run_id: uuid.UUID
    generation_id: uuid.UUID
    binding_id: uuid.UUID
    project_id: uuid.UUID
    project_alias_id: uuid.UUID
    sections: tuple[SectionResult, ...]
    registry_version_id: uuid.UUID
    registry_activation_id: uuid.UUID
    source_bundle_sha256: str
    source_record_count: int
    baseline_high_water_mark: str
    registry_sha256: str
    dish_release: str
    honest_release: str
    protocol_release: str
    protocol_sha256: str
    schema_release: str
    schema_sha256: str
    schema_head: str
    source_generation: str

    def as_json(self) -> dict[str, object]:
        return _jsonify(asdict(self))


def _jsonify(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(record: Mapping[str, Any], field: str, *, line_number: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InitialBootstrapError(f"source line {line_number}: {field} must be a nonblank string")
    return value


def _parse_observed_at(value: object, *, line_number: int) -> datetime:
    if not isinstance(value, str):
        raise InitialBootstrapError(f"source line {line_number}: observed_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InitialBootstrapError(
            f"source line {line_number}: observed_at is not valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InitialBootstrapError(f"source line {line_number}: observed_at must include an offset")
    return parsed


def inspect_source_bundle(
    path: Path,
    *,
    project_id: uuid.UUID,
) -> SourceBundle:
    """Bind bootstrap metadata to one exact importer NDJSON corpus.

    Every distinct section referenced by the corpus is discovered here, not
    supplied by the caller: a real corpus spans many sections.
    """
    source = path.expanduser().resolve()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise InitialBootstrapError(f"unable to read source bundle: {source}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    task_ids: set[uuid.UUID] = set()
    asana_gids: set[str] = set()
    observed_values: list[datetime] = []
    sections: dict[uuid.UUID, str] = {}
    section_names: dict[uuid.UUID, str] = {}
    count = 0
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        if not raw_line.strip():
            continue
        count += 1
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InitialBootstrapError(f"source line {line_number}: invalid JSON") from exc
        if not isinstance(record, dict):
            raise InitialBootstrapError(f"source line {line_number}: record must be a JSON object")
        try:
            task_id = uuid.UUID(_required_string(record, "task_id", line_number=line_number))
        except ValueError as exc:
            raise InitialBootstrapError(f"source line {line_number}: task_id is not a UUID") from exc
        if task_id in task_ids:
            raise InitialBootstrapError(f"source line {line_number}: duplicate task_id {task_id}")
        task_ids.add(task_id)
        asana_gid = _required_string(record, "asana_task_gid", line_number=line_number)
        if not asana_gid.isdigit() or asana_gid.startswith("0"):
            raise InitialBootstrapError(
                f"source line {line_number}: asana_task_gid is not a canonical positive decimal GID"
            )
        if asana_gid in asana_gids:
            raise InitialBootstrapError(f"source line {line_number}: duplicate Asana GID {asana_gid}")
        asana_gids.add(asana_gid)
        projects = record.get("project_ids")
        if not isinstance(projects, list):
            raise InitialBootstrapError(f"source line {line_number}: project_ids must be an array")
        try:
            parsed_projects = tuple(uuid.UUID(str(value)) for value in projects)
        except ValueError as exc:
            raise InitialBootstrapError(f"source line {line_number}: project_ids contains a non-UUID") from exc
        if parsed_projects != (project_id,):
            raise InitialBootstrapError(
                f"source line {line_number}: project_ids must be exactly [{project_id}]"
            )
        try:
            parsed_section = uuid.UUID(_required_string(record, "section_id", line_number=line_number))
        except ValueError as exc:
            raise InitialBootstrapError(f"source line {line_number}: section_id is not a UUID") from exc
        section_gid = _required_string(record, "section_gid", line_number=line_number)
        raw_section_name = record.get("section_name")
        section_name: str | None = None
        if raw_section_name is not None:
            if not isinstance(raw_section_name, str) or not raw_section_name.strip():
                raise InitialBootstrapError(
                    f"source line {line_number}: section_name must be a nonblank string when present"
                )
            section_name = raw_section_name.strip()
            if len(section_name) > 256:
                raise InitialBootstrapError(
                    f"source line {line_number}: section_name exceeds 256 characters"
                )
        known_gid = sections.get(parsed_section)
        if known_gid is None:
            sections[parsed_section] = section_gid
        elif known_gid != section_gid:
            raise InitialBootstrapError(
                f"source line {line_number}: section_id {parsed_section} maps to both "
                f"section_gid {known_gid} and {section_gid}"
            )
        if section_name is not None:
            known_name = section_names.get(parsed_section)
            if known_name is None:
                section_names[parsed_section] = section_name
            elif known_name != section_name:
                raise InitialBootstrapError(
                    f"source line {line_number}: section_id {parsed_section} maps to both "
                    f"section_name {known_name!r} and {section_name!r}"
                )
        _required_string(record, "title", line_number=line_number)
        _required_string(record, "body", line_number=line_number)
        _required_string(record, "identity_scheme", line_number=line_number)
        _required_string(record, "content_identity", line_number=line_number)
        try:
            operation_history_from_mapping(record)
        except ValueError as exc:
            raise InitialBootstrapError(
                f"source line {line_number}: invalid operation history: {exc}"
            ) from exc
        if not isinstance(record.get("completed"), bool):
            raise InitialBootstrapError(f"source line {line_number}: completed must be a boolean")
        observed_values.append(_parse_observed_at(record.get("observed_at"), line_number=line_number))
    if count == 0:
        raise InitialBootstrapError("source bundle contains no importer records")
    return SourceBundle(
        path=source,
        sha256=digest,
        record_count=count,
        max_observed_at=max(observed_values),
        sections=sections,
        section_names=section_names,
        explicit_operation_revocations=True,
    )


def git_head(repo: Path) -> str:
    root = repo.expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = str(getattr(exc, "stderr", "") or "").strip()
        suffix = f": {detail}" if detail else ""
        raise InitialBootstrapError(f"unable to resolve Git HEAD for {root}{suffix}") from exc
    return completed.stdout.strip()


def require_git_head(repo: Path, expected_commit: str) -> str:
    actual = git_head(repo)
    if actual != expected_commit:
        raise InitialBootstrapError(
            f"Git HEAD mismatch for {repo.expanduser().resolve()}: expected {expected_commit}, got {actual}"
        )
    return actual


def resolve_honest_checkout(
    repo: Path,
    *,
    expected_commit: str,
    expected_protocol_version: str = SUPPORTED_PROTOCOL_VERSION,
    expected_schema_version: str = SUPPORTED_TASK_SCHEMA_VERSION,
) -> HonestCheckout:
    root = repo.expanduser().resolve()
    commit = require_git_head(root, expected_commit)
    version_path = root / DISH_VERSION_FILENAME
    try:
        versions = parse_dish_version(version_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InitialBootstrapError(f"unable to read {version_path}") from exc
    schema_path = root / TASK_SCHEMA_FILENAME
    try:
        schema_raw = schema_path.read_bytes()
        schema_value = json.loads(schema_raw)
    except OSError as exc:
        raise InitialBootstrapError(f"unable to read {schema_path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitialBootstrapError(f"invalid JSON in {schema_path}") from exc
    schema = validate_task_schema_shape(schema_value, filename=TASK_SCHEMA_FILENAME)
    protocol_version = versions["PROTOCOL_VERSION"]
    schema_version = versions["SCHEMA_VERSION"]
    if protocol_version != expected_protocol_version:
        raise InitialBootstrapError(
            f"Honest protocol version mismatch: expected {expected_protocol_version}, got {protocol_version}"
        )
    if schema_version != expected_schema_version:
        raise InitialBootstrapError(
            f"Honest schema version mismatch: expected {expected_schema_version}, got {schema_version}"
        )
    if schema["protocol_version"] != protocol_version:
        raise InitialBootstrapError("dish-task-schema.json protocol_version disagrees with DISH_VERSION")
    if schema["schema_version"] != schema_version:
        raise InitialBootstrapError("dish-task-schema.json schema_version disagrees with DISH_VERSION")
    protocol_files: dict[str, dict[str, str]] = {}
    for role in sorted(PROTOCOL_FILENAMES):
        relative = schema["protocol_files"].get(role)
        if not isinstance(relative, str) or not relative:
            raise InitialBootstrapError(f"task schema does not name the {role} protocol file")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise InitialBootstrapError(f"{role} protocol escapes Honest checkout: {relative}") from exc
        try:
            digest = _file_sha256(path)
        except OSError as exc:
            raise InitialBootstrapError(f"unable to hash {role} protocol: {path}") from exc
        protocol_files[role] = {"path": relative, "sha256": digest}
    governed_paths = [DISH_VERSION_FILENAME, TASK_SCHEMA_FILENAME]
    governed_paths.extend(item["path"] for item in protocol_files.values())
    try:
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", *governed_paths],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InitialBootstrapError("unable to verify Honest governed assets against HEAD") from exc
    if dirty:
        raise InitialBootstrapError(f"Honest governed assets differ from HEAD: {dirty}")
    protocol_bundle = {
        "format": "honest-protocol-bundle-sha256-v1",
        "protocol_version": protocol_version,
        "files": protocol_files,
    }
    return HonestCheckout(
        root=root,
        commit=commit,
        protocol_version=protocol_version,
        schema_version=schema_version,
        protocol_sha256=_canonical_json_sha256(protocol_bundle),
        schema_sha256=hashlib.sha256(schema_raw).hexdigest(),
        protocol_files=protocol_files,
    )


def _registry_payload(spec: InitialBootstrapSpec) -> dict[str, object]:
    return {
        "format": "dish-section-registry-v1",
        "project": {
            "project_id": str(spec.project_id),
            "logical_name": spec.project_name,
            "external_system": "asana",
            "external_id": spec.project_gid,
        },
        "sections": [
            {
                "section_id": str(section.section_id),
                "logical_name": section.section_name,
                "display_name": section.section_name,
                "workflow_role": section.workflow_role,
                "ordinal": ordinal,
                "external_system": "asana",
                "external_id": section.section_gid,
            }
            for ordinal, section in enumerate(spec.sections)
        ],
    }


def _require_empty_target(session: Session) -> None:
    tables = (
        models.ImportRun,
        models.AuthorityGeneration,
        models.HonestContractBinding,
        models.GovernedProject,
        models.GovernedSection,
        models.SectionRegistryVersion,
        models.SectionRegistryActivation,
        models.ActiveSectionRegistry,
    )
    occupied = [
        model.__tablename__
        for model in tables
        if int(session.scalar(select(func.count()).select_from(model)) or 0) != 0
    ]
    if occupied:
        raise InitialBootstrapError(
            "first-generation bootstrap requires empty authority tables; occupied="
            + ",".join(occupied)
        )


def bootstrap_initial_generation(
    session: Session,
    spec: InitialBootstrapSpec,
    *,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> InitialBootstrapResult:
    """Atomically create the first active generation and one-section registry."""
    _require_empty_target(session)
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise InitialBootstrapError("bootstrap clock must return a timezone-aware datetime")
    authority = AuthorityRepository(session)
    contracts = ContractBindingRepository(session)
    registry = RegistryRepository(session)

    import_run_id = uuid_factory()
    generation_id = uuid_factory()
    binding_id = uuid_factory()
    project_alias_id = uuid_factory()
    registry_version_id = uuid_factory()
    registry_activation_id = uuid_factory()
    registry_sha256 = _canonical_json_sha256(_registry_payload(spec))

    authority.add_import_run(
        models.ImportRun(
            import_run_id=import_run_id,
            source_commit=spec.dish_commit,
            source_release=spec.dish_release,
            legacy_generation_id=spec.source_generation,
            baseline_high_water_mark=spec.source_bundle.high_water_mark,
            source_bundle_sha256=spec.source_bundle.sha256,
            status="complete",
            started_at=now,
            completed_at=now,
            provenance={
                "resolved_by": "dish-pg-bootstrap-initial",
                "source_path": str(spec.source_bundle.path),
                "source_record_count": spec.source_bundle.record_count,
                "source_bundle_hash_method": "sha256-file-bytes",
                **(
                    {EXACT_REVOCATION_HISTORY_PROVENANCE_KEY: EXACT_REVOCATION_SOURCE_CONTRACT}
                    if spec.source_bundle.explicit_operation_revocations
                    else {}
                ),
                "project_id": str(spec.project_id),
                "section_ids": [str(section.section_id) for section in spec.sections],
            },
        )
    )
    authority.add_generation(
        models.AuthorityGeneration(
            generation_id=generation_id,
            predecessor_generation_id=None,
            creation_reason="initial_cutover",
            external_restore_control_id=None,
            schema_head=spec.schema_head,
            dish_release=spec.dish_release,
            status="active",
            created_at=now,
            retired_at=None,
        )
    )
    contracts.add(
        models.HonestContractBinding(
            binding_id=binding_id,
            binding_kind="release",
            source_identity=spec.honest_release,
            dish_release=spec.dish_release,
            honest_release=spec.honest_release,
            protocol_release=spec.honest.protocol_version,
            protocol_sha256=spec.honest.protocol_sha256,
            schema_release=spec.honest.schema_version,
            schema_sha256=spec.honest.schema_sha256,
            migration_id=None,
            source_schema_version=None,
            target_schema_version=None,
            migration_metadata_sha256=None,
            source_ids={
                "repo": "honest-pantry",
                "commit": spec.honest.commit,
                "dish_version": DISH_VERSION_FILENAME,
                "schema_file": TASK_SCHEMA_FILENAME,
                "protocol_files": spec.honest.protocol_files,
            },
            provenance={
                "resolved_by": "dish-pg-bootstrap-initial",
                "honest_root": str(spec.honest.root),
                "protocol_hash_method": "sha256-canonical-protocol-file-digest-manifest-v1",
                "schema_hash_method": "sha256-file-bytes",
            },
            resolved_at=now,
        )
    )
    registry.add_project(
        models.GovernedProject(
            project_id=spec.project_id,
            logical_name=spec.project_name,
            lifecycle="active",
            import_run_id=import_run_id,
            created_at=now,
            retired_at=None,
        )
    )
    registry.add_project_alias(
        models.ProjectExternalAlias(
            alias_id=project_alias_id,
            project_id=spec.project_id,
            external_system="asana",
            external_id=spec.project_gid,
            origin="imported",
            import_run_id=import_run_id,
            projection_event_id=None,
            state="active",
            created_at=now,
            retired_at=None,
        )
    )
    section_results: list[SectionResult] = []
    for section in spec.sections:
        section_alias_id = uuid_factory()
        registry.add_section(
            models.GovernedSection(
                section_id=section.section_id,
                project_id=spec.project_id,
                logical_name=section.section_name,
                lifecycle="active",
                import_run_id=import_run_id,
                created_at=now,
                retired_at=None,
            )
        )
        registry.add_section_alias(
            models.SectionExternalAlias(
                alias_id=section_alias_id,
                section_id=section.section_id,
                external_system="asana",
                external_id=section.section_gid,
                origin="imported",
                import_run_id=import_run_id,
                projection_event_id=None,
                state="active",
                created_at=now,
                retired_at=None,
            )
        )
        section_results.append(
            SectionResult(
                section_id=section.section_id,
                section_gid=section.section_gid,
                section_alias_id=section_alias_id,
            )
        )
    registry.add_registry_version(
        models.SectionRegistryVersion(
            registry_version_id=registry_version_id,
            generation_id=generation_id,
            version_number=1,
            import_run_id=import_run_id,
            contract_binding_id=binding_id,
            registry_sha256=registry_sha256,
            created_at=now,
        ),
        [
            models.SectionRegistryEntry(
                registry_version_id=registry_version_id,
                section_id=section.section_id,
                ordinal=ordinal,
                display_name=section.section_name,
                workflow_role=section.workflow_role,
            )
            for ordinal, section in enumerate(spec.sections)
        ],
    )
    registry.activate_registry(
        activation=models.SectionRegistryActivation(
            registry_activation_id=registry_activation_id,
            generation_id=generation_id,
            registry_version_id=registry_version_id,
            activation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            registry_revision=1,
            activated_at=now,
        ),
        current=models.ActiveSectionRegistry(
            generation_id=generation_id,
            registry_version_id=registry_version_id,
            registry_activation_id=registry_activation_id,
            registry_revision=1,
            updated_at=now,
        ),
    )
    return InitialBootstrapResult(
        import_run_id=import_run_id,
        generation_id=generation_id,
        binding_id=binding_id,
        project_id=spec.project_id,
        project_alias_id=project_alias_id,
        sections=tuple(section_results),
        registry_version_id=registry_version_id,
        registry_activation_id=registry_activation_id,
        source_bundle_sha256=spec.source_bundle.sha256,
        source_record_count=spec.source_bundle.record_count,
        baseline_high_water_mark=spec.source_bundle.high_water_mark,
        registry_sha256=registry_sha256,
        dish_release=spec.dish_release,
        honest_release=spec.honest_release,
        protocol_release=spec.honest.protocol_version,
        protocol_sha256=spec.honest.protocol_sha256,
        schema_release=spec.honest.schema_version,
        schema_sha256=spec.honest.schema_sha256,
        schema_head=spec.schema_head,
        source_generation=spec.source_generation,
    )


def require_postgresql_target(engine: Engine, *, expected_database_name: str, schema_head: str) -> None:
    url = make_url(str(engine.url))
    if url.get_backend_name() != "postgresql":
        raise InitialBootstrapError("initial-generation bootstrap requires a PostgreSQL target")
    if url.database != expected_database_name:
        raise InitialBootstrapError(
            f"database target mismatch: expected {expected_database_name!r}, got {url.database!r}"
        )
    with engine.connect() as connection:
        versions = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if versions != [schema_head]:
        raise InitialBootstrapError(
            f"Alembic head mismatch: expected [{schema_head!r}], got {versions!r}"
        )


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-bootstrap-initial")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", default=DEFAULT_DATABASE_NAME)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-generation", required=True)
    parser.add_argument("--dish-repo", required=True, type=Path)
    parser.add_argument("--dish-commit", default=DEFAULT_DISH_COMMIT)
    parser.add_argument("--honest-repo", required=True, type=Path)
    parser.add_argument("--honest-commit", default=DEFAULT_HONEST_COMMIT)
    parser.add_argument("--schema-head", default=DEFAULT_SCHEMA_HEAD)
    parser.add_argument("--protocol-version", default=SUPPORTED_PROTOCOL_VERSION)
    parser.add_argument("--task-schema-version", default=SUPPORTED_TASK_SCHEMA_VERSION)
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--project-gid", required=True)
    parser.add_argument("--project-name", default="Cooking")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--research-queue-section-id",
        type=uuid.UUID,
        default=None,
        help="designate this discovered section as the Research Queue (workflow_role=research_queue)",
    )
    parser.add_argument("--research-queue-section-gid")
    parser.add_argument(
        "--verification-queue-section-id",
        type=uuid.UUID,
        default=None,
        help="designate this discovered section as the Verification Queue (workflow_role=verification_queue)",
    )
    parser.add_argument("--verification-queue-section-gid")
    parser.add_argument("--sourcing-section-id", type=uuid.UUID)
    parser.add_argument("--sourcing-section-gid")
    parser.add_argument("--reference-section-id", type=uuid.UUID)
    parser.add_argument("--reference-section-gid")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        require_git_head(args.dish_repo, args.dish_commit)
        honest = resolve_honest_checkout(
            args.honest_repo,
            expected_commit=args.honest_commit,
            expected_protocol_version=args.protocol_version,
            expected_schema_version=args.task_schema_version,
        )
        source_bundle = inspect_source_bundle(
            args.source,
            project_id=args.project_id,
        )
        sections = section_specs_from_bundle(source_bundle)
        if args.research_queue_section_id is not None:
            if args.research_queue_section_gid is not None:
                sections = ensure_required_section(
                    sections,
                    section_id=args.research_queue_section_id,
                    section_gid=args.research_queue_section_gid,
                    section_name="Research Queue",
                )
            sections = apply_research_queue_role(
                sections, research_queue_section_id=args.research_queue_section_id
            )
        if args.verification_queue_section_id is not None:
            if args.verification_queue_section_id == args.research_queue_section_id:
                raise InitialBootstrapError(
                    "Research Queue and Verification Queue must be different sections"
                )
            if args.verification_queue_section_gid is not None:
                sections = ensure_required_section(
                    sections,
                    section_id=args.verification_queue_section_id,
                    section_gid=args.verification_queue_section_gid,
                    section_name="Verification Queue",
                )
            sections = apply_verification_queue_role(
                sections,
                verification_queue_section_id=args.verification_queue_section_id,
            )
        for section_id, section_gid, section_name in (
            (args.sourcing_section_id, args.sourcing_section_gid, "Sourcing"),
            (args.reference_section_id, args.reference_section_gid, "Reference"),
        ):
            if (section_id is None) != (section_gid is None):
                raise InitialBootstrapError(
                    f"{section_name} section ID and GID must be supplied together"
                )
            if section_id is not None and section_gid is not None:
                sections = ensure_required_section(
                    sections,
                    section_id=section_id,
                    section_gid=section_gid,
                    section_name=section_name,
                )
        spec = InitialBootstrapSpec(
            dish_commit=args.dish_commit,
            schema_head=args.schema_head,
            source_generation=args.source_generation,
            source_bundle=source_bundle,
            honest=honest,
            project_id=args.project_id,
            project_gid=args.project_gid,
            project_name=args.project_name,
            sections=sections,
        )
        engine = create_database_engine(DatabaseSettings(url=args.database_url))
        try:
            require_postgresql_target(
                engine,
                expected_database_name=args.expected_database_name,
                schema_head=args.schema_head,
            )
            factory = session_factory(engine)
            with session_scope(factory) as session:
                result = bootstrap_initial_generation(session, spec)
        finally:
            engine.dispose()
        receipt = result.as_json()
        if args.receipt is not None:
            _atomic_json(args.receipt, receipt)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
