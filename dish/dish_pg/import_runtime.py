"""Real runtime hooks and a direct database wrapper for legacy NDJSON import."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .importer import SourceRecord, iter_source, run_import
from .release_history import (
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_SOURCE_CONTRACT,
)
from .repositories import CoreAuthorityError


class ImportRuntimeError(ValueError):
    """The bootstrapped import target or imported result is inconsistent."""


def prepare_import_run(
    session: Session,
    generation_id: UUID,
    import_run_id: UUID,
    contract_binding_id: UUID,
) -> None:
    """Verify that bootstrap already created every importer precondition."""
    generation = session.get(models.AuthorityGeneration, generation_id)
    if generation is None or generation.status != "active":
        raise CoreAuthorityError("import requires the bootstrapped active generation")
    import_run = session.get(models.ImportRun, import_run_id)
    if import_run is None or import_run.status != "complete":
        raise CoreAuthorityError("import requires the bootstrapped complete import run")
    provenance = import_run.provenance if isinstance(import_run.provenance, dict) else {}
    if (
        provenance.get(EXACT_REVOCATION_HISTORY_PROVENANCE_KEY)
        != EXACT_REVOCATION_SOURCE_CONTRACT
    ):
        raise CoreAuthorityError(
            "import run does not attest an explicit exact-operation revocation source"
        )
    binding = session.get(models.HonestContractBinding, contract_binding_id)
    if binding is None or binding.dish_release != generation.dish_release:
        raise CoreAuthorityError("import requires the bootstrapped matching contract binding")
    active = session.get(models.ActiveSectionRegistry, generation_id)
    if active is None:
        raise CoreAuthorityError("import requires the bootstrapped active section registry")
    version = session.get(models.SectionRegistryVersion, active.registry_version_id)
    if version is None:
        raise CoreAuthorityError("active section registry points to a missing version")
    if version.import_run_id != import_run_id or version.contract_binding_id != contract_binding_id:
        raise CoreAuthorityError("active section registry is not bound to this import run and contract")


def already_imported(session: Session, task_id: UUID) -> bool:
    """Return true only when the exact stable task identity already exists."""
    return session.get(models.DishTask, task_id) is not None


def _source_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_records(path: Path) -> tuple[SourceRecord, ...]:
    records = tuple(iter_source(path))
    if not records:
        raise ImportRuntimeError("source contains no importer records")
    malformed = [record.identifier for record in records if record.error is not None or record.spec is None]
    if malformed:
        raise ImportRuntimeError(f"source contains malformed records: {malformed}")
    return records


def verify_imported_records(
    session: Session,
    *,
    records: tuple[SourceRecord, ...],
    generation_id: UUID,
    import_run_id: UUID,
    contract_binding_id: UUID,
) -> list[str]:
    """Compare every imported authority head with its source record."""
    errors: list[str] = []
    for record in records:
        assert record.spec is not None
        spec = record.spec
        prefix = f"task_id={spec.task_id}"
        task = session.get(models.DishTask, spec.task_id)
        if task is None:
            errors.append(f"{prefix}: missing DishTask")
            continue
        if task.import_run_id != import_run_id or task.creation_route != "import":
            errors.append(f"{prefix}: DishTask provenance mismatch")
        alias = session.scalar(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.task_id == spec.task_id,
                models.TaskExternalAlias.external_system == "asana",
                models.TaskExternalAlias.external_id == spec.asana_task_gid,
                models.TaskExternalAlias.state == "active",
            )
        )
        if alias is None or alias.import_run_id != import_run_id:
            errors.append(f"{prefix}: TaskExternalAlias mismatch")
        version = session.scalar(
            select(models.ContentVersion).where(
                models.ContentVersion.generation_id == generation_id,
                models.ContentVersion.task_id == spec.task_id,
                models.ContentVersion.content_identity == spec.content_identity,
            )
        )
        if version is None:
            errors.append(f"{prefix}: missing ContentVersion")
        elif (
            version.title != spec.title
            or version.body != spec.body
            or version.identity_scheme != spec.identity_scheme
            or version.contract_binding_id != contract_binding_id
            or version.import_run_id != import_run_id
        ):
            errors.append(f"{prefix}: ContentVersion content/provenance mismatch")
        placement = session.get(models.CurrentTaskSectionPlacement, (generation_id, spec.task_id))
        if placement is None or placement.section_id != spec.section_id:
            errors.append(f"{prefix}: current section placement mismatch")
        completion = session.get(models.CurrentTaskCompletion, (generation_id, spec.task_id))
        if completion is None or completion.completed != spec.completed:
            errors.append(f"{prefix}: current completion mismatch")
        memberships = tuple(
            session.scalars(
                select(models.CurrentTaskProjectMembership).where(
                    models.CurrentTaskProjectMembership.generation_id == generation_id,
                    models.CurrentTaskProjectMembership.task_id == spec.task_id,
                    models.CurrentTaskProjectMembership.is_member.is_(True),
                )
            )
        )
        if {row.project_id for row in memberships} != set(spec.project_ids):
            errors.append(f"{prefix}: current project membership mismatch")

        history = spec.operation_history
        imported_operations = tuple(
            session.scalars(
                select(wf.WorkflowOperation).where(
                    wf.WorkflowOperation.generation_id == generation_id,
                    wf.WorkflowOperation.task_id == spec.task_id,
                    wf.WorkflowOperation.import_run_id == import_run_id,
                )
            )
        )
        if {row.operation_id for row in imported_operations} != {item.operation_id for item in history.operations}:
            errors.append(f"{prefix}: imported workflow operation identity set mismatch")
        for source in history.operations:
            target = session.get(wf.WorkflowOperation, source.operation_id)
            expected_lifecycle = (
                "completed"
                if source.status == "completed"
                else "abandoned"
                if source.terminal_outcome in {"agent_abandoned", "safe_reclaimed"}
                else "cancelled_by_marco"
            )
            if target is None or (
                target.import_run_id != import_run_id
                or target.creation_request_id is not None
                or target.creation_execution_id is not None
                or target.kind != source.kind
                or target.lifecycle != expected_lifecycle
                or target.phase != "terminal"
                or target.terminal_outcome != source.terminal_outcome
            ):
                errors.append(f"{prefix}: imported WorkflowOperation mismatch {source.operation_id}")

        imported_cycles = tuple(
            session.scalars(
                select(wf.VerificationCycle).where(
                    wf.VerificationCycle.generation_id == generation_id,
                    wf.VerificationCycle.task_id == spec.task_id,
                    wf.VerificationCycle.import_run_id == import_run_id,
                )
            )
        )
        if {row.cycle_id for row in imported_cycles} != {item.cycle_id for item in history.verification_cycles}:
            errors.append(f"{prefix}: imported verification cycle identity set mismatch")
        for source in history.verification_cycles:
            target = session.get(wf.VerificationCycle, source.cycle_id)
            if target is None or (
                target.import_run_id != import_run_id
                or target.created_by_execution_id is not None
                or target.reviewed_content_version_id is not None
                or target.operation_id != source.operation_id
                or target.cycle_sequence != source.cycle_sequence
                or target.outcome != source.outcome
            ):
                errors.append(f"{prefix}: imported VerificationCycle mismatch {source.cycle_id}")

        imported_leases = tuple(
            session.scalars(
                select(wf.ServiceLease).where(
                    wf.ServiceLease.generation_id == generation_id,
                    wf.ServiceLease.task_id == spec.task_id,
                    wf.ServiceLease.import_run_id == import_run_id,
                )
            )
        )
        if {row.lease_id for row in imported_leases} != {item.lease_id for item in history.leases}:
            errors.append(f"{prefix}: imported service lease identity set mismatch")
        for source in history.leases:
            target = session.get(wf.ServiceLease, source.lease_id)
            if target is None or (
                target.import_run_id != import_run_id
                or target.run_id is not None
                or target.source_run_id != source.source_run_id
                or target.operation_id != source.operation_id
                or target.owner_id != source.owner_id
                or target.lease_kind != source.lease_kind
                or target.actor_attempt_sequence != source.actor_attempt_sequence
                or target.verification_cycle_id != source.verification_cycle_id
                or target.state != "released"
            ):
                errors.append(f"{prefix}: imported ServiceLease mismatch {source.lease_id}")

        imported_revocations = tuple(
            session.scalars(
                select(wf.OperationRunRevocation).where(
                    wf.OperationRunRevocation.generation_id == generation_id,
                    wf.OperationRunRevocation.operation_id.in_(
                        [item.operation_id for item in history.operations]
                    ),
                    wf.OperationRunRevocation.import_run_id == import_run_id,
                )
            )
        )
        if {row.revocation_id for row in imported_revocations} != {
            item.revocation_id for item in history.revocations
        }:
            errors.append(f"{prefix}: imported exact operation/run revocation identity set mismatch")
        for source in history.revocations:
            target = session.get(wf.OperationRunRevocation, source.revocation_id)
            if target is None or (
                target.import_run_id != import_run_id
                or target.run_id is not None
                or target.source_run_id != source.source_run_id
                or target.operation_id != source.operation_id
                or target.owner_id != source.owner_id
                or target.source_lease_id != source.source_lease_id
                or target.reason != source.reason
            ):
                errors.append(
                    f"{prefix}: imported OperationRunRevocation mismatch {source.revocation_id}"
                )
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-import-legacy")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-record-count", type=int)
    parser.add_argument("--generation-id", required=True, type=UUID)
    parser.add_argument("--import-run-id", required=True, type=UUID)
    parser.add_argument("--contract-binding-id", required=True, type=UUID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    try:
        before_sha256 = _source_sha256(source)
        if before_sha256 != args.expected_source_sha256:
            raise ImportRuntimeError(
                f"source SHA256 mismatch: expected {args.expected_source_sha256}, got {before_sha256}"
            )
        records = _source_records(source)
        if args.expected_record_count is not None and len(records) != args.expected_record_count:
            raise ImportRuntimeError(
                f"source record count mismatch: expected {args.expected_record_count}, got {len(records)}"
            )
        engine = create_database_engine(DatabaseSettings(url=args.database_url))
        try:
            factory = session_factory(engine)
            with session_scope(factory) as session:
                prepare_import_run(
                    session,
                    args.generation_id,
                    args.import_run_id,
                    args.contract_binding_id,
                )
                import_run = session.get(models.ImportRun, args.import_run_id)
                assert import_run is not None
                if import_run.source_bundle_sha256 != before_sha256:
                    raise ImportRuntimeError(
                        "bootstrapped ImportRun source_bundle_sha256 does not match the source"
                    )
                recorded_count = import_run.provenance.get("source_record_count")
                if recorded_count != len(records):
                    raise ImportRuntimeError(
                        f"bootstrapped ImportRun record count mismatch: expected {recorded_count}, got {len(records)}"
                    )
            summary = run_import(
                source=source,
                generation_id=args.generation_id,
                import_run_id=args.import_run_id,
                contract_binding_id=args.contract_binding_id,
                session_factory=factory,
                prepare_import_run=prepare_import_run,
                already_imported=already_imported,
            )
            after_sha256 = _source_sha256(source)
            if after_sha256 != before_sha256:
                raise ImportRuntimeError("source changed while import was running")
            with session_scope(factory) as session:
                verification_errors = verify_imported_records(
                    session,
                    records=records,
                    generation_id=args.generation_id,
                    import_run_id=args.import_run_id,
                    contract_binding_id=args.contract_binding_id,
                )
        finally:
            engine.dispose()
        result = {
            "source_sha256": before_sha256,
            "source_records": len(records),
            "imported": summary.imported,
            "skipped": summary.skipped,
            "failed": summary.failed,
            "verified_records": len(records) if not verification_errors else 0,
            "verification_complete": not verification_errors,
            "verification_errors": verification_errors,
        }
        print(json.dumps(result, sort_keys=True, indent=2))
        complete = (
            summary.failed == 0
            and summary.imported + summary.skipped == len(records)
            and not verification_errors
        )
        return 0 if complete else 1
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
