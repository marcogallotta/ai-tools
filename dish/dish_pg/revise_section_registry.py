"""One-shot operational trigger for the correction-ImportRun registry-role revision.

``revise-section-registry`` is a retained admin command (see
``command_port.py``) with no legacy equivalent, so it can never be reached
through ordinary shadow replay. This module invokes it directly against a
PostgreSQL target through ``PostgresCommandPort``, the same execution path
the live service and shadow worker use, outside of any legacy-originated
request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from . import models
from .bootstrap import require_postgresql_target
from .command_port import CommandCall, CommandResult, PostgresCommandPort
from .location_manifest import target_uuid
from .repositories import AuthorityRepository, RegistryRepository, registry_source_import_run
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .workflow import WorkflowAuthorityService


class ReviseSectionRegistryError(ValueError):
    """The target, arguments, or result of the registry-role correction is unsafe."""


def revise_section_registry(
    session,
    *,
    research_queue_section_id: uuid.UUID,
    verification_queue_section_id: uuid.UUID,
    owner_id: str,
    agent: str,
    cursor_secret: bytes,
    now: datetime,
    uuid_factory=uuid.uuid4,
) -> CommandResult:
    if research_queue_section_id == verification_queue_section_id:
        raise ReviseSectionRegistryError(
            "Research Queue and Verification Queue must be different sections"
        )
    generation_id = session.scalar(
        select(models.AuthorityGeneration.generation_id).where(
            models.AuthorityGeneration.status == "active"
        )
    )
    if generation_id is None:
        raise ReviseSectionRegistryError("no active authority generation")
    run_id = uuid_factory()
    capability_digest = hashlib.sha256(
        f"registry-role-correction:{owner_id}:{agent}:{run_id}:{now.isoformat()}".encode()
    ).digest()
    WorkflowAuthorityService(session, uuid_factory=uuid_factory).register_run(
        run_id=run_id,
        generation_id=generation_id,
        owner_id=owner_id,
        agent=agent,
        capability_digest=capability_digest,
        registered_at=now,
    )
    port = PostgresCommandPort(session, cursor_secret=cursor_secret, uuid_factory=uuid_factory)
    return port.execute(
        CommandCall(
            command_name="revise-section-registry",
            arguments={
                "research_queue_section_id": str(research_queue_section_id),
                "verification_queue_section_id": str(verification_queue_section_id),
            },
            owner_id=owner_id,
            principal_class="admin",
            run_id=run_id,
            request_id=uuid_factory(),
            now=now,
        )
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, sort_keys=True, indent=2, default=str)
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
    parser = argparse.ArgumentParser(prog="dish-pg-revise-section-registry")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--schema-head", required=True)
    parser.add_argument("--research-queue-section-id", type=uuid.UUID, required=True)
    parser.add_argument("--verification-queue-section-id", type=uuid.UUID, required=True)
    parser.add_argument("--owner-id", default="Marco")
    parser.add_argument(
        "--agent",
        default="marco",
        choices=("claude", "gpt", "codex", "marco", "service"),
    )
    parser.add_argument("--cursor-secret-env", default="DISH_PG_CURSOR_SECRET")
    parser.add_argument("--receipt", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cursor_secret = os.environ.get(args.cursor_secret_env, "").encode()
    if len(cursor_secret) < 24:
        print(
            json.dumps(
                {"error": f"{args.cursor_secret_env} must be set to at least 24 bytes"}
            )
        )
        return 2
    try:
        engine = create_database_engine(DatabaseSettings(url=args.database_url))
        try:
            require_postgresql_target(
                engine,
                expected_database_name=args.expected_database_name,
                schema_head=args.schema_head,
            )
            factory = session_factory(engine)
            now = datetime.now(timezone.utc)
            with session_scope(factory) as session:
                result = revise_section_registry(
                    session,
                    research_queue_section_id=args.research_queue_section_id,
                    verification_queue_section_id=args.verification_queue_section_id,
                    owner_id=args.owner_id,
                    agent=args.agent,
                    cursor_secret=cursor_secret,
                    now=now,
                )
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - reported verbatim to the operator
        report = {"error": str(exc), "type": type(exc).__name__}
        if args.receipt is not None:
            _atomic_json(args.receipt, report)
        print(json.dumps(report, sort_keys=True))
        return 2
    receipt = {
        "ok": result.ok,
        "command": result.command,
        "code": result.code,
        "http_status": result.http_status,
        "data": dict(result.data),
    }
    if args.receipt is not None:
        _atomic_json(args.receipt, receipt)
    print(json.dumps(receipt, sort_keys=True, indent=2, default=str))
    return 0 if result.ok else 1


TEST_DATABASE_NAME = "dish_stage_a_test"
SOURCE_RELEASE = "test-registry-membership-revision-v1"
REVISION_KIND = "test_section_membership"
RECEIPT_OPERATION = "revise-test-section-registry-membership"


class ReviseTestSectionRegistryMembershipError(ValueError):
    """The requested TEST registry membership revision is unsafe or stale."""


def _canonical_gid(value: str, *, field: str) -> str:
    gid = str(value or "").strip()
    if not gid.isdigit() or gid.startswith("0"):
        raise ReviseTestSectionRegistryMembershipError(
            f"{field} must be a canonical positive decimal Asana GID"
        )
    return gid


def _targets(
    research_queue_section_gid: str,
    verification_queue_section_gid: str,
    sourcing_section_gid: str,
    reference_section_gid: str,
) -> tuple[dict[str, Any], ...]:
    gids = (
        _canonical_gid(research_queue_section_gid, field="research_queue_section_gid"),
        _canonical_gid(
            verification_queue_section_gid, field="verification_queue_section_gid"
        ),
        _canonical_gid(sourcing_section_gid, field="sourcing_section_gid"),
        _canonical_gid(reference_section_gid, field="reference_section_gid"),
    )
    if len(set(gids)) != 4:
        raise ReviseTestSectionRegistryMembershipError(
            "the four required TEST sections must have distinct Asana GIDs"
        )
    names = ("Research Queue", "Verification Queue", "Sourcing", "Reference")
    roles = (
        "research_queue",
        "verification_queue",
        f"imported-section-{gids[2]}",
        f"imported-section-{gids[3]}",
    )
    return tuple(
        {"external_id": gid, "display_name": name, "workflow_role": role, "ordinal": ordinal}
        for ordinal, (gid, name, role) in enumerate(zip(gids, names, roles, strict=True))
    )


def require_test_database_url(database_url: str, *, expected_database_name: str) -> None:
    """Fail closed before connect unless the URL names exact PostgreSQL TEST."""
    if expected_database_name != TEST_DATABASE_NAME:
        raise ReviseTestSectionRegistryMembershipError(
            f"operation requires expected database {TEST_DATABASE_NAME!r}"
        )
    if not database_url.strip():
        raise ReviseTestSectionRegistryMembershipError("database URL must be set explicitly")
    try:
        url = make_url(database_url)
    except Exception as exc:  # noqa: BLE001 - normalize operator target failure
        raise ReviseTestSectionRegistryMembershipError("database URL is not valid") from exc
    if url.get_backend_name() != "postgresql":
        raise ReviseTestSectionRegistryMembershipError("operation requires PostgreSQL")
    database = url.database or ""
    if "prod" in database.lower():
        raise ReviseTestSectionRegistryMembershipError(
            "operation refuses database names containing 'prod'"
        )
    if database != TEST_DATABASE_NAME:
        raise ReviseTestSectionRegistryMembershipError(
            f"operation requires exact TEST database {TEST_DATABASE_NAME!r}, got {database!r}"
        )


def _require_connected_test(session) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        raise ReviseTestSectionRegistryMembershipError("operation requires PostgreSQL")
    database = str(session.scalar(text("SELECT current_database()")) or "")
    if "prod" in database.lower() or database != TEST_DATABASE_NAME:
        raise ReviseTestSectionRegistryMembershipError(
            f"connected database must be exact TEST database {TEST_DATABASE_NAME!r}, got {database!r}"
        )


def _locked_scalar(session, statement):
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.scalar(statement.execution_options(populate_existing=True))


def _entries(session, registry_version_id: uuid.UUID) -> tuple[models.SectionRegistryEntry, ...]:
    return tuple(
        session.scalars(
            select(models.SectionRegistryEntry)
            .where(models.SectionRegistryEntry.registry_version_id == registry_version_id)
            .order_by(models.SectionRegistryEntry.ordinal)
        )
    )


def _single_active_alias(session, *, kind: str, internal_id: uuid.UUID):
    model = models.SectionExternalAlias if kind == "section" else models.ProjectExternalAlias
    key = model.section_id if kind == "section" else model.project_id
    rows = tuple(
        session.scalars(
            select(model).where(
                key == internal_id,
                model.external_system == "asana",
                model.state == "active",
            )
        )
    )
    if len(rows) != 1:
        raise ReviseTestSectionRegistryMembershipError(
            f"governed {kind} {internal_id} must have exactly one active Asana identity"
        )
    return rows[0]


def _current_membership(session, version_id: uuid.UUID):
    result: dict[str, tuple[models.GovernedSection, models.SectionRegistryEntry]] = {}
    project_ids: set[uuid.UUID] = set()
    for entry in _entries(session, version_id):
        section = session.get(models.GovernedSection, entry.section_id)
        if section is None or section.lifecycle != "active":
            raise ReviseTestSectionRegistryMembershipError(
                "active registry contains a missing or retired governed section"
            )
        alias = _single_active_alias(session, kind="section", internal_id=section.section_id)
        result[alias.external_id] = (section, entry)
        project_ids.add(section.project_id)
    if len(project_ids) != 1:
        raise ReviseTestSectionRegistryMembershipError(
            "active registry does not resolve one governed project"
        )
    return next(iter(project_ids)), result


def _find_section(session, *, external_id: str) -> models.GovernedSection | None:
    alias = session.scalar(
        select(models.SectionExternalAlias).where(
            models.SectionExternalAlias.external_system == "asana",
            models.SectionExternalAlias.external_id == external_id,
            models.SectionExternalAlias.state == "active",
        )
    )
    if alias is None:
        return None
    section = session.get(models.GovernedSection, alias.section_id)
    if section is None or section.lifecycle != "active":
        raise ReviseTestSectionRegistryMembershipError(
            f"Asana section {external_id} resolves to a missing or retired governed section"
        )
    return section


def _receipt_sections(resolved: tuple[tuple[models.GovernedSection, dict[str, Any]], ...]):
    return [
        {
            "section_id": str(section.section_id),
            "section_gid": target["external_id"],
            "name": target["display_name"],
            "workflow_role": target["workflow_role"],
            "ordinal": target["ordinal"],
        }
        for section, target in resolved
    ]


def _result(
    *,
    changed: bool,
    already_applied: bool,
    generation_id: uuid.UUID,
    import_run_id: uuid.UUID | None,
    service_run_id: uuid.UUID | None,
    before_version_id: uuid.UUID,
    before_revision: int,
    after_version_id: uuid.UUID,
    after_revision: int,
    resolved: tuple[tuple[models.GovernedSection, dict[str, Any]], ...],
) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": RECEIPT_OPERATION,
        "changed": changed,
        "already_applied": already_applied,
        "generation_id": str(generation_id),
        "import_run_id": str(import_run_id) if import_run_id else None,
        "service_run_id": str(service_run_id) if service_run_id else None,
        "before": {
            "registry_version_id": str(before_version_id),
            "registry_revision": before_revision,
        },
        "after": {
            "registry_version_id": str(after_version_id),
            "registry_revision": after_revision,
            "sections": _receipt_sections(resolved),
        },
    }


def _exact_retry(
    session,
    *,
    current_version: models.SectionRegistryVersion,
    current_revision: int,
    expected_generation_id: uuid.UUID,
    expected_registry_version_id: uuid.UUID,
    expected_registry_revision: int,
    targets: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    import_run = session.get(models.ImportRun, current_version.import_run_id)
    provenance = (
        import_run.provenance
        if import_run is not None and isinstance(import_run.provenance, dict)
        else {}
    )
    if (
        import_run is None
        or import_run.source_release != SOURCE_RELEASE
        or provenance.get("revision_kind") != REVISION_KIND
        or provenance.get("generation_id") != str(expected_generation_id)
        or provenance.get("predecessor_registry_version_id")
        != str(expected_registry_version_id)
        or provenance.get("predecessor_registry_revision") != expected_registry_revision
        or provenance.get("target_sections") != list(targets)
        or provenance.get("result_registry_sha256") != current_version.registry_sha256
        or provenance.get("source_record_count") != 0
    ):
        return None
    try:
        service_run_id = uuid.UUID(str(provenance["service_run_id"]))
    except (KeyError, TypeError, ValueError):
        return None
    _project_id, current = _current_membership(session, current_version.registry_version_id)
    if set(current) != {target["external_id"] for target in targets}:
        return None
    resolved = []
    for target in targets:
        section, entry = current[target["external_id"]]
        if (
            entry.display_name != target["display_name"]
            or entry.workflow_role != target["workflow_role"]
            or entry.ordinal != target["ordinal"]
        ):
            return None
        resolved.append((section, target))
    return _result(
        changed=False,
        already_applied=True,
        generation_id=expected_generation_id,
        import_run_id=import_run.import_run_id,
        service_run_id=service_run_id,
        before_version_id=expected_registry_version_id,
        before_revision=expected_registry_revision,
        after_version_id=current_version.registry_version_id,
        after_revision=current_revision,
        resolved=tuple(resolved),
    )


def revise_test_section_registry_membership(
    session,
    *,
    target_database_name: str,
    expected_generation_id: uuid.UUID,
    expected_registry_version_id: uuid.UUID,
    expected_registry_revision: int,
    research_queue_section_gid: str,
    verification_queue_section_gid: str,
    sourcing_section_gid: str,
    reference_section_gid: str,
    owner_id: str,
    agent: str,
    now: datetime,
    uuid_factory=uuid.uuid4,
) -> dict[str, Any]:
    """Create and activate one exact four-section successor registry in connected TEST."""
    _require_connected_test(session)
    return _revise_test_section_registry_membership_transaction(
        session,
        target_database_name=target_database_name,
        expected_generation_id=expected_generation_id,
        expected_registry_version_id=expected_registry_version_id,
        expected_registry_revision=expected_registry_revision,
        research_queue_section_gid=research_queue_section_gid,
        verification_queue_section_gid=verification_queue_section_gid,
        sourcing_section_gid=sourcing_section_gid,
        reference_section_gid=reference_section_gid,
        owner_id=owner_id,
        agent=agent,
        now=now,
        uuid_factory=uuid_factory,
    )


def _revise_test_section_registry_membership_transaction(
    session,
    *,
    target_database_name: str,
    expected_generation_id: uuid.UUID,
    expected_registry_version_id: uuid.UUID,
    expected_registry_revision: int,
    research_queue_section_gid: str,
    verification_queue_section_gid: str,
    sourcing_section_gid: str,
    reference_section_gid: str,
    owner_id: str,
    agent: str,
    now: datetime,
    uuid_factory=uuid.uuid4,
) -> dict[str, Any]:
    """Apply the membership transaction after the connected TEST fence has passed."""
    if target_database_name != TEST_DATABASE_NAME:
        raise ReviseTestSectionRegistryMembershipError(
            f"operation requires exact TEST target {TEST_DATABASE_NAME!r}"
        )
    if expected_registry_revision < 1:
        raise ReviseTestSectionRegistryMembershipError("expected registry revision must be positive")
    targets = _targets(
        research_queue_section_gid,
        verification_queue_section_gid,
        sourcing_section_gid,
        reference_section_gid,
    )

    generation = _locked_scalar(
        session,
        select(models.AuthorityGeneration).where(models.AuthorityGeneration.status == "active"),
    )
    if generation is None:
        raise ReviseTestSectionRegistryMembershipError("no active authority generation")
    if generation.generation_id != expected_generation_id:
        raise ReviseTestSectionRegistryMembershipError(
            f"stale active generation: expected {expected_generation_id}, found {generation.generation_id}"
        )
    active = _locked_scalar(
        session,
        select(models.ActiveSectionRegistry).where(
            models.ActiveSectionRegistry.generation_id == generation.generation_id
        ),
    )
    if active is None:
        raise ReviseTestSectionRegistryMembershipError(
            "active generation has no active section registry"
        )
    current_version = session.get(models.SectionRegistryVersion, active.registry_version_id)
    if current_version is None:
        raise ReviseTestSectionRegistryMembershipError("active registry version is missing")

    if (
        active.registry_version_id != expected_registry_version_id
        or active.registry_revision != expected_registry_revision
    ):
        retry = _exact_retry(
            session,
            current_version=current_version,
            current_revision=active.registry_revision,
            expected_generation_id=expected_generation_id,
            expected_registry_version_id=expected_registry_version_id,
            expected_registry_revision=expected_registry_revision,
            targets=targets,
        )
        if retry is not None:
            return retry
        raise ReviseTestSectionRegistryMembershipError(
            "stale active registry: expected version/revision "
            f"{expected_registry_version_id}/{expected_registry_revision}, found "
            f"{active.registry_version_id}/{active.registry_revision}"
        )

    predecessor_revision = active.registry_revision
    project_id, current = _current_membership(session, current_version.registry_version_id)
    target_ids = {target["external_id"] for target in targets}
    if not set(current).issubset(target_ids):
        raise ReviseTestSectionRegistryMembershipError(
            "membership revision refuses to remove existing registry sections: "
            + ", ".join(sorted(set(current) - target_ids))
        )
    project = session.get(models.GovernedProject, project_id)
    if project is None or project.lifecycle != "active":
        raise ReviseTestSectionRegistryMembershipError("active registry project is not active")
    project_alias = _single_active_alias(session, kind="project", internal_id=project_id)

    resolved: list[tuple[models.GovernedSection | None, dict[str, Any]]] = []
    for target in targets:
        section = _find_section(session, external_id=target["external_id"])
        if section is not None:
            if section.project_id != project_id or section.logical_name != target["display_name"]:
                raise ReviseTestSectionRegistryMembershipError(
                    f"Asana section {target['external_id']} does not match the expected governed section"
                )
        else:
            by_name = session.scalar(
                select(models.GovernedSection).where(
                    models.GovernedSection.project_id == project_id,
                    models.GovernedSection.logical_name == target["display_name"],
                )
            )
            if by_name is not None:
                raise ReviseTestSectionRegistryMembershipError(
                    f"{target['display_name']} already exists without the supplied exact Asana identity"
                )
        resolved.append((section, target))

    if len(current) == 4 and all(section is not None for section, _ in resolved):
        exact = all(
            target["external_id"] in current
            and current[target["external_id"]][1].display_name == target["display_name"]
            and current[target["external_id"]][1].workflow_role == target["workflow_role"]
            and current[target["external_id"]][1].ordinal == target["ordinal"]
            for _section, target in resolved
        )
        if exact:
            final = tuple((section, target) for section, target in resolved if section is not None)
            return _result(
                changed=False,
                already_applied=False,
                generation_id=generation.generation_id,
                import_run_id=current_version.import_run_id,
                service_run_id=None,
                before_version_id=current_version.registry_version_id,
                before_revision=predecessor_revision,
                after_version_id=current_version.registry_version_id,
                after_revision=predecessor_revision,
                resolved=final,
            )

    source_import_run = registry_source_import_run(session, current_version)
    service_run_id = uuid_factory()
    WorkflowAuthorityService(session, uuid_factory=uuid_factory).register_run(
        run_id=service_run_id,
        generation_id=generation.generation_id,
        owner_id=owner_id,
        agent=agent,
        capability_digest=hashlib.sha256(
            (
                f"test-registry-membership:{owner_id}:{agent}:{service_run_id}:"
                f"{generation.generation_id}:{current_version.registry_version_id}:"
                f"{predecessor_revision}"
            ).encode()
        ).digest(),
        registered_at=now,
    )

    import_run_id = uuid_factory()
    final: list[tuple[models.GovernedSection, dict[str, Any]]] = []
    for section, target in resolved:
        if section is None:
            section_id = target_uuid("section", target["external_id"])
            if session.get(models.GovernedSection, section_id) is not None:
                raise ReviseTestSectionRegistryMembershipError(
                    f"derived section identity {section_id} is already in use"
                )
            section = models.GovernedSection(
                section_id=section_id,
                project_id=project_id,
                logical_name=target["display_name"],
                lifecycle="active",
                import_run_id=import_run_id,
                created_at=now,
                retired_at=None,
            )
        final.append((section, target))

    registry_payload = {
        "format": "dish-section-registry-v1",
        "project": {
            "project_id": str(project.project_id),
            "logical_name": project.logical_name,
            "external_system": "asana",
            "external_id": project_alias.external_id,
        },
        "sections": [
            {
                "section_id": str(section.section_id),
                "logical_name": section.logical_name,
                "display_name": target["display_name"],
                "workflow_role": target["workflow_role"],
                "ordinal": target["ordinal"],
                "external_system": "asana",
                "external_id": target["external_id"],
            }
            for section, target in final
        ],
    }
    registry_sha256 = hashlib.sha256(
        json.dumps(registry_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    revision_payload = {
        "format": "dish-test-registry-membership-revision-v1",
        "generation_id": str(generation.generation_id),
        "predecessor_registry_version_id": str(current_version.registry_version_id),
        "predecessor_registry_revision": predecessor_revision,
        "source_import_run_id": str(source_import_run.import_run_id),
        "service_run_id": str(service_run_id),
        "target_sections": list(targets),
        "result_registry_sha256": registry_sha256,
    }
    revision_sha256 = hashlib.sha256(
        json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    AuthorityRepository(session).add_import_run(
        models.ImportRun(
            import_run_id=import_run_id,
            source_commit=source_import_run.source_commit,
            source_release=SOURCE_RELEASE,
            legacy_generation_id=source_import_run.legacy_generation_id,
            baseline_high_water_mark=(
                f"test-registry-membership:{current_version.registry_version_id}:"
                f"{predecessor_revision}:{registry_sha256}"
            ),
            source_bundle_sha256=revision_sha256,
            status="complete",
            started_at=now,
            completed_at=now,
            provenance={
                "revision_kind": REVISION_KIND,
                "revision_bundle_sha256": revision_sha256,
                "generation_id": str(generation.generation_id),
                "predecessor_registry_version_id": str(current_version.registry_version_id),
                "predecessor_registry_revision": predecessor_revision,
                "source_import_run_id": str(source_import_run.import_run_id),
                "service_run_id": str(service_run_id),
                "owner_id": owner_id,
                "agent": agent,
                "target_sections": list(targets),
                "result_registry_sha256": registry_sha256,
                "source_record_count": 0,
            },
        )
    )

    registry = RegistryRepository(session)
    for original, (section, target) in zip(resolved, final, strict=True):
        if original[0] is not None:
            continue
        registry.add_section(section)
        registry.add_section_alias(
            models.SectionExternalAlias(
                alias_id=uuid_factory(),
                section_id=section.section_id,
                external_system="asana",
                external_id=target["external_id"],
                origin="imported",
                import_run_id=import_run_id,
                projection_event_id=None,
                state="active",
                created_at=now,
                retired_at=None,
            )
        )

    new_version_id, activation_id = uuid_factory(), uuid_factory()
    next_revision = predecessor_revision + 1
    registry.add_registry_version(
        models.SectionRegistryVersion(
            registry_version_id=new_version_id,
            generation_id=generation.generation_id,
            version_number=current_version.version_number + 1,
            import_run_id=import_run_id,
            contract_binding_id=current_version.contract_binding_id,
            registry_sha256=registry_sha256,
            created_at=now,
        ),
        (
            models.SectionRegistryEntry(
                registry_version_id=new_version_id,
                section_id=section.section_id,
                ordinal=target["ordinal"],
                display_name=target["display_name"],
                workflow_role=target["workflow_role"],
            )
            for section, target in final
        ),
    )
    registry.activate_registry(
        activation=models.SectionRegistryActivation(
            registry_activation_id=activation_id,
            generation_id=generation.generation_id,
            registry_version_id=new_version_id,
            activation_route="import",
            import_run_id=import_run_id,
            command_execution_id=None,
            registry_revision=next_revision,
            activated_at=now,
        ),
        current=models.ActiveSectionRegistry(
            generation_id=generation.generation_id,
            registry_version_id=new_version_id,
            registry_activation_id=activation_id,
            registry_revision=next_revision,
            updated_at=now,
        ),
    )
    return _result(
        changed=True,
        already_applied=False,
        generation_id=generation.generation_id,
        import_run_id=import_run_id,
        service_run_id=service_run_id,
        before_version_id=current_version.registry_version_id,
        before_revision=predecessor_revision,
        after_version_id=new_version_id,
        after_revision=next_revision,
        resolved=tuple(final),
    )

def _membership_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-revise-test-section-registry-membership")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--schema-head", required=True)
    parser.add_argument("--expected-generation-id", type=uuid.UUID, required=True)
    parser.add_argument("--expected-registry-version-id", type=uuid.UUID, required=True)
    parser.add_argument("--expected-registry-revision", type=int, required=True)
    parser.add_argument("--research-queue-section-gid", required=True)
    parser.add_argument("--verification-queue-section-gid", required=True)
    parser.add_argument("--sourcing-section-gid", required=True)
    parser.add_argument("--reference-section-gid", required=True)
    parser.add_argument("--owner-id", default="Marco")
    parser.add_argument(
        "--agent", default="marco", choices=("claude", "gpt", "codex", "marco", "service")
    )
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def membership_main(argv: list[str] | None = None) -> int:
    args = _membership_parser().parse_args(argv)
    try:
        require_test_database_url(
            args.database_url, expected_database_name=args.expected_database_name
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
                _require_connected_test(session)
                result = revise_test_section_registry_membership(
                    session,
                    target_database_name=args.expected_database_name,
                    expected_generation_id=args.expected_generation_id,
                    expected_registry_version_id=args.expected_registry_version_id,
                    expected_registry_revision=args.expected_registry_revision,
                    research_queue_section_gid=args.research_queue_section_gid,
                    verification_queue_section_gid=args.verification_queue_section_gid,
                    sourcing_section_gid=args.sourcing_section_gid,
                    reference_section_gid=args.reference_section_gid,
                    owner_id=args.owner_id,
                    agent=args.agent,
                    now=datetime.now(timezone.utc),
                )
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - preserve exact operator failure
        report = {"ok": False, "error": str(exc), "type": type(exc).__name__}
        _atomic_json(args.receipt, report)
        print(json.dumps(report, sort_keys=True))
        return 2
    _atomic_json(args.receipt, result)
    print(json.dumps(result, sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
