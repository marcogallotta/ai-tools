"""Shared terminal-history / release-candidate boundary invariants."""
from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf

TERMINAL_HISTORY_IMPORT_KIND = "terminal-history-backfill-v1"
SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT = (
    "candidate-authority-v3+supplemental-terminal-history-v1"
)
EXACT_REVOCATION_HISTORY_PROVENANCE_KEY = "operation_run_revocation_history"
EXACT_REVOCATION_SOURCE_CONTRACT = "explicit-operation-run-revocations-v1"
EXACT_REVOCATION_RECONCILIATION_CONTRACT = (
    "reconciled-operation-run-revocations-v1"
)
EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY = "reconciled_operation_ids"
EXACT_REVOCATION_SNAPSHOT_FORMAT = "dish-terminal-history-backfill-source-v2"


def _provenance(run: models.ImportRun) -> Mapping[str, object]:
    return run.provenance if isinstance(run.provenance, Mapping) else {}


def _primary_import_run_id(run: models.ImportRun) -> uuid.UUID | None:
    provenance = _provenance(run)
    if provenance.get("import_kind") != TERMINAL_HISTORY_IMPORT_KIND:
        return run.import_run_id
    value = provenance.get("primary_import_run_id")
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _reconciled_operation_ids(run: models.ImportRun) -> frozenset[uuid.UUID] | None:
    value = _provenance(run).get(EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY)
    if not isinstance(value, list):
        return None
    parsed: list[uuid.UUID] = []
    try:
        parsed = [uuid.UUID(str(item)) for item in value]
    except (TypeError, ValueError):
        return None
    if len(parsed) != len(set(parsed)):
        return None
    return frozenset(parsed)


def legacy_imported_operation_ids(
    session: Session,
    *,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    primary_import_run_id: uuid.UUID,
) -> frozenset[uuid.UUID]:
    operation_ids: set[uuid.UUID] = set()
    operations = session.scalars(
        select(wf.WorkflowOperation).where(
            wf.WorkflowOperation.generation_id == generation_id,
            wf.WorkflowOperation.task_id == task_id,
            wf.WorkflowOperation.import_run_id.is_not(None),
        )
    )
    for operation in operations:
        assert operation.import_run_id is not None
        imported_by = session.get(models.ImportRun, operation.import_run_id)
        if imported_by is not None and _primary_import_run_id(imported_by) == primary_import_run_id:
            operation_ids.add(operation.operation_id)
    return frozenset(operation_ids)


def exact_revocation_reconciliation_matches(
    run: models.ImportRun,
    *,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    primary_import_run_id: uuid.UUID,
    operation_id: uuid.UUID | None = None,
) -> bool:
    provenance = _provenance(run)
    reconciled_operation_ids = _reconciled_operation_ids(run)
    return (
        run.status == "complete"
        and provenance.get("import_kind") == TERMINAL_HISTORY_IMPORT_KIND
        and provenance.get(EXACT_REVOCATION_HISTORY_PROVENANCE_KEY)
        == EXACT_REVOCATION_RECONCILIATION_CONTRACT
        and provenance.get("generation_id") == str(generation_id)
        and provenance.get("task_id") == str(task_id)
        and provenance.get("primary_import_run_id") == str(primary_import_run_id)
        and provenance.get("source_format") == EXACT_REVOCATION_SNAPSHOT_FORMAT
        and provenance.get("source_record_count") == 1
        and provenance.get("source_bundle_hash_method") == "sha256-file-bytes"
        and provenance.get("candidate_attestation")
        == SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT
        and reconciled_operation_ids is not None
        and (operation_id is None or operation_id in reconciled_operation_ids)
    )



def task_revocation_history_reconciled(
    session: Session,
    *,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    primary_import_run_id: uuid.UUID,
) -> bool:
    primary = session.get(models.ImportRun, primary_import_run_id)
    if primary is None or primary.status != "complete":
        return False
    provenance = _provenance(primary)
    if (
        provenance.get(EXACT_REVOCATION_HISTORY_PROVENANCE_KEY)
        == EXACT_REVOCATION_SOURCE_CONTRACT
    ):
        return True
    required_operation_ids = legacy_imported_operation_ids(
        session,
        generation_id=generation_id,
        task_id=task_id,
        primary_import_run_id=primary_import_run_id,
    )
    if not required_operation_ids:
        return True
    covered_operation_ids: set[uuid.UUID] = set()
    candidates = session.scalars(
        select(models.ImportRun).where(
            models.ImportRun.status == "complete",
            models.ImportRun.legacy_generation_id == primary.legacy_generation_id,
        )
    )
    for candidate in candidates:
        if exact_revocation_reconciliation_matches(
            candidate,
            generation_id=generation_id,
            task_id=task_id,
            primary_import_run_id=primary_import_run_id,
        ):
            covered_operation_ids.update(_reconciled_operation_ids(candidate) or ())
    return required_operation_ids.issubset(covered_operation_ids)

def operation_revocation_history_reconciled(
    session: Session, *, operation: wf.WorkflowOperation
) -> bool:
    """Return whether an imported operation has explicit revocation provenance.

    Native PostgreSQL operations have no legacy omission risk. Imported operations
    must either come from the revocation-aware source contract or have a later
    task-scoped supplemental reconciliation captured from the legacy SQLite source.
    """

    if operation.import_run_id is None:
        return True
    imported_by = session.get(models.ImportRun, operation.import_run_id)
    if imported_by is None or imported_by.status != "complete":
        return False
    provenance = _provenance(imported_by)
    if (
        provenance.get(EXACT_REVOCATION_HISTORY_PROVENANCE_KEY)
        == EXACT_REVOCATION_SOURCE_CONTRACT
    ):
        return True
    if (
        provenance.get(EXACT_REVOCATION_HISTORY_PROVENANCE_KEY)
        == EXACT_REVOCATION_RECONCILIATION_CONTRACT
    ):
        primary = _primary_import_run_id(imported_by)
        return primary is not None and exact_revocation_reconciliation_matches(
            imported_by,
            generation_id=operation.generation_id,
            task_id=operation.task_id,
            primary_import_run_id=primary,
            operation_id=operation.operation_id,
        )

    primary = _primary_import_run_id(imported_by)
    if primary is None:
        return False
    return task_revocation_history_reconciled(
        session,
        generation_id=operation.generation_id,
        task_id=operation.task_id,
        primary_import_run_id=primary,
    )


def acquire_generation_release_gate(
    session: Session, *, generation_id: uuid.UUID
) -> models.AuthorityGeneration | None:
    """Serialize terminal-history application with candidate validation per generation.

    PostgreSQL holds the AuthorityGeneration row lock until the caller-owned transaction
    ends.  Other dialects still perform a fresh identity-map refresh so focused tests
    exercise the same re-read semantics without claiming PostgreSQL lock certification.
    """

    statement = select(models.AuthorityGeneration).where(
        models.AuthorityGeneration.generation_id == generation_id
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    statement = statement.execution_options(populate_existing=True)
    return session.scalar(statement)
