"""Deterministic builder for the release-candidate authority manifest."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import MetaData, String, Table, cast, inspect, select
from sqlalchemy.orm import Session

from . import candidate_manifest_models as manifest_models
from . import models
from . import stage3_models as wf
from . import stage5_models as tx
from . import stage6_models as rel
from .release_evidence import ReleaseAuthorityError, sha256_json
from .release_history import (
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY,
    SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT,
    TERMINAL_HISTORY_IMPORT_KIND,
    exact_revocation_reconciliation_matches,
)

MANIFEST_VERSION = 3
BUILDER_CONTRACT_VERSION = "candidate-authority-v3"

COMPONENT_FIELDS = (
    "mapping_membership_sha256",
    "import_completion_sha256",
    "typed_import_linkage_sha256",
    "reconciliation_evidence_sha256",
)

_UUID_HEX_RE = re.compile(r"[0-9a-fA-F]{32}\Z")


def _canonical_value(value: Any, *, column_name: str | None = None) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(member)
            for key, member in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(member) for member in value]
    if isinstance(value, bytes):
        return value.hex()
    if column_name is not None and column_name.endswith("_id") and isinstance(value, str):
        candidate = value.strip()
        try:
            if _UUID_HEX_RE.fullmatch(candidate):
                return str(uuid.UUID(hex=candidate))
            return str(uuid.UUID(candidate))
        except ValueError:
            return value
    return value


def _canonical_row(row: Mapping[str, Any], expected_columns: Sequence[str]) -> dict[str, Any]:
    return {
        column: _canonical_value(row.get(column), column_name=column)
        for column in expected_columns
    }


def _uuid_text(value: uuid.UUID, dialect_name: str) -> str:
    return value.hex if dialect_name == "sqlite" else str(value)


def _uuid_match(column: Any, value: uuid.UUID, dialect_name: str) -> Any:
    return cast(column, String) == _uuid_text(value, dialect_name)


def _uuid_membership(column: Any, values: Sequence[uuid.UUID], dialect_name: str) -> Any:
    return cast(column, String).in_([_uuid_text(value, dialect_name) for value in values])


def _table_rows(
    session: Session,
    *,
    table_name: str,
    expected_columns: Sequence[str],
    where: Callable[[Table, str], Any] | None = None,
) -> dict[str, Any]:
    bind = session.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table_name):
        return {
            "table": table_name,
            "present": False,
            "columns_present": [],
            "rows": [],
        }
    table = Table(table_name, MetaData(), autoload_with=bind)
    available = [column for column in expected_columns if column in table.c]
    statement = select(*(table.c[column] for column in available))
    if where is not None:
        statement = statement.where(where(table, bind.dialect.name))
    rows = [
        _canonical_row(dict(result), expected_columns)
        for result in session.execute(statement).mappings().all()
    ]
    rows.sort(key=sha256_json)
    return {
        "table": table_name,
        "present": True,
        "columns_present": available,
        "rows": rows,
    }


def _single_row(payload: dict[str, Any], *, label: str) -> dict[str, Any] | None:
    rows = payload["rows"]
    if len(rows) > 1:
        raise ReleaseAuthorityError(f"candidate manifest {label} is not unique")
    return rows[0] if rows else None


def _mapping_membership_digest(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> str:
    specifications = (
        (
            "project_projection_mappings",
            (
                "mapping_id",
                "generation_id",
                "projection_epoch_id",
                "project_id",
                "alias_id",
                "state",
                "mapping_revision",
                "bound_at",
                "retired_at",
            ),
        ),
        (
            "section_projection_mappings",
            (
                "mapping_id",
                "generation_id",
                "projection_epoch_id",
                "section_id",
                "alias_id",
                "state",
                "mapping_revision",
                "bound_at",
                "retired_at",
            ),
        ),
        (
            "task_projection_mappings",
            (
                "mapping_id",
                "generation_id",
                "projection_epoch_id",
                "task_id",
                "alias_id",
                "state",
                "mapping_revision",
                "bound_at",
                "retired_at",
            ),
        ),
    )
    tables = []
    for table_name, columns in specifications:
        tables.append(
            _table_rows(
                session,
                table_name=table_name,
                expected_columns=columns,
                where=lambda table, dialect: (
                    _uuid_match(table.c.generation_id, candidate.generation_id, dialect)
                    & _uuid_match(
                        table.c.projection_epoch_id,
                        candidate.projection_epoch_id,
                        dialect,
                    )
                    & (table.c.state == "active")
                ),
            )
        )
    return sha256_json(
        {
            "contract": "active-projection-mapping-membership-v1",
            "generation_id": str(candidate.generation_id),
            "projection_epoch_id": str(candidate.projection_epoch_id),
            "tables": tables,
        }
    )


def _import_completion_digest(
    session: Session, *, candidate: rel.ReleaseCandidate, source_import_run_id: uuid.UUID
) -> str:
    batch = _table_rows(
        session,
        table_name="source_import_batches",
        expected_columns=(
            "import_batch_id",
            "generation_id",
            "import_run_id",
            "source_release",
            "source_commit",
            "source_database_sha256",
            "source_sidecars",
            "ledger_through_commit",
            "expected_entities",
            "imported_entities",
            "status",
            "started_at",
            "completed_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.import_batch_id, candidate.source_import_batch_id, dialect
        ),
    )
    import_run = _table_rows(
        session,
        table_name="stage_a_import_runs",
        expected_columns=(
            "import_run_id",
            "source_commit",
            "source_release",
            "legacy_generation_id",
            "baseline_high_water_mark",
            "source_bundle_sha256",
            "status",
            "started_at",
            "completed_at",
            "provenance",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.import_run_id, source_import_run_id, dialect
        ),
    )
    if _single_row(batch, label="source import batch") is None:
        raise ReleaseAuthorityError("candidate manifest source import batch is missing")
    if _single_row(import_run, label="source import run") is None:
        raise ReleaseAuthorityError("candidate manifest source import run is missing")
    return sha256_json(
        {
            "contract": "source-import-completion-v1",
            "batch": batch,
            "import_run": import_run,
        }
    )


_SUPPLEMENTAL_PROVENANCE_FIELDS = (
    "import_kind",
    "generation_id",
    "task_id",
    "legacy_task_gid",
    "primary_import_run_id",
    "source_format",
    "source_record_count",
    "source_bundle_hash_method",
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY,
    "candidate_attestation",
)

_SUPPLEMENTAL_HISTORY_TABLES = (
    (
        "workflow_operations",
        (
            "operation_id",
            "generation_id",
            "task_id",
            "kind",
            "lifecycle",
            "phase",
            "persisted_actions",
            "import_run_id",
            "creation_request_id",
            "creation_execution_id",
            "contract_binding_id",
            "predecessor_operation_id",
            "terminal_outcome",
            "operation_revision",
            "created_at",
            "terminal_at",
        ),
    ),
    (
        "verification_cycles",
        (
            "cycle_id",
            "generation_id",
            "task_id",
            "operation_id",
            "reviewed_content_version_id",
            "contract_binding_id",
            "cycle_sequence",
            "lifecycle",
            "outcome",
            "import_run_id",
            "created_by_execution_id",
            "created_at",
            "terminal_at",
        ),
    ),
    (
        "service_leases",
        (
            "lease_id",
            "generation_id",
            "task_id",
            "operation_id",
            "run_id",
            "import_run_id",
            "source_run_id",
            "owner_id",
            "lease_kind",
            "actor_role",
            "actor_attempt_sequence",
            "verification_cycle_id",
            "state",
            "issued_at",
            "expires_at",
            "lease_revision",
            "terminal_at",
        ),
    ),
    (
        "operation_run_revocations",
        (
            "revocation_id",
            "generation_id",
            "operation_id",
            "owner_id",
            "run_id",
            "import_run_id",
            "source_run_id",
            "source_lease_id",
            "reason",
            "revoked_at",
        ),
    ),
)


def _validate_supplemental_revocation_scope(
    session: Session,
    *,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    reconciled_operation_ids: object,
    history_tables: Sequence[Mapping[str, Any]],
) -> None:
    if reconciled_operation_ids is not None:
        if not isinstance(reconciled_operation_ids, list):
            raise ReleaseAuthorityError(
                "candidate supplemental exact-revocation operation coverage is malformed"
            )
        seen: set[uuid.UUID] = set()
        for value in reconciled_operation_ids:
            try:
                operation_uuid = uuid.UUID(str(value))
            except (TypeError, ValueError) as exc:
                raise ReleaseAuthorityError(
                    "candidate supplemental exact-revocation operation coverage is malformed"
                ) from exc
            if operation_uuid in seen:
                raise ReleaseAuthorityError(
                    "candidate supplemental exact-revocation operation coverage is duplicated"
                )
            seen.add(operation_uuid)
            operation = session.get(wf.WorkflowOperation, operation_uuid)
            if (
                operation is None
                or operation.generation_id != generation_id
                or operation.task_id != task_id
            ):
                raise ReleaseAuthorityError(
                    "candidate supplemental exact-revocation operation coverage is outside its task/generation provenance"
                )

    revocations = next(
        (table for table in history_tables if table["table"] == "operation_run_revocations"),
        None,
    )
    if revocations is None:
        return
    for row in revocations["rows"]:
        operation_id = row.get("operation_id")
        try:
            operation_uuid = uuid.UUID(str(operation_id))
        except (TypeError, ValueError) as exc:
            raise ReleaseAuthorityError(
                "candidate supplemental exact revocation has invalid operation identity"
            ) from exc
        operation = session.get(wf.WorkflowOperation, operation_uuid)
        if (
            operation is None
            or operation.generation_id != generation_id
            or operation.task_id != task_id
        ):
            raise ReleaseAuthorityError(
                "candidate supplemental exact revocation is outside its task/generation provenance"
            )


def _supplemental_terminal_history_digest(
    session: Session,
    *,
    candidate: rel.ReleaseCandidate,
    source_import_run_id: uuid.UUID,
) -> str | None:
    primary = session.get(models.ImportRun, source_import_run_id)
    if primary is None:
        raise ReleaseAuthorityError("candidate manifest source import run is missing")

    generation_text = str(candidate.generation_id)
    primary_text = str(source_import_run_id)
    supplemental_imports: list[dict[str, Any]] = []
    for run in session.scalars(select(models.ImportRun)).all():
        provenance = run.provenance if isinstance(run.provenance, Mapping) else {}
        if provenance.get("import_kind") != TERMINAL_HISTORY_IMPORT_KIND:
            continue
        if provenance.get("generation_id") != generation_text:
            continue
        if provenance.get("primary_import_run_id") != primary_text:
            raise ReleaseAuthorityError(
                "candidate supplemental terminal-history import has mismatched primary lineage"
            )
        if run.status != "complete" or run.legacy_generation_id != primary.legacy_generation_id:
            raise ReleaseAuthorityError(
                "candidate supplemental terminal-history import provenance is incomplete"
            )

        history_tables = [
            _table_rows(
                session,
                table_name=table_name,
                expected_columns=columns,
                where=lambda table, dialect, import_run_id=run.import_run_id: _uuid_match(
                    table.c.import_run_id, import_run_id, dialect
                ),
            )
            for table_name, columns in _SUPPLEMENTAL_HISTORY_TABLES
        ]
        try:
            provenance_task_id = uuid.UUID(str(provenance.get("task_id")))
        except (TypeError, ValueError) as exc:
            raise ReleaseAuthorityError(
                "candidate supplemental terminal-history import has invalid task provenance"
            ) from exc
        _validate_supplemental_revocation_scope(
            session,
            generation_id=candidate.generation_id,
            task_id=provenance_task_id,
            reconciled_operation_ids=provenance.get(
                EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY
            ),
            history_tables=history_tables,
        )
        exact_reconciliation = exact_revocation_reconciliation_matches(
            run,
            generation_id=candidate.generation_id,
            task_id=provenance_task_id,
            primary_import_run_id=source_import_run_id,
        )
        revocation_rows = next(
            table["rows"]
            for table in history_tables
            if table["table"] == "operation_run_revocations"
        )
        if revocation_rows and not exact_reconciliation:
            raise ReleaseAuthorityError(
                "candidate supplemental exact revocations lack reconciliation provenance"
            )
        if not any(table["rows"] for table in history_tables) and not exact_reconciliation:
            raise ReleaseAuthorityError(
                "candidate supplemental terminal-history import has no imported history"
            )
        supplemental_imports.append(
            {
                "import_run": {
                    "import_run_id": str(run.import_run_id),
                    "source_commit": run.source_commit,
                    "source_release": run.source_release,
                    "legacy_generation_id": run.legacy_generation_id,
                    "baseline_high_water_mark": run.baseline_high_water_mark,
                    "source_bundle_sha256": run.source_bundle_sha256,
                    "status": run.status,
                    "started_at": _canonical_value(run.started_at),
                    "completed_at": _canonical_value(run.completed_at),
                    "provenance": {
                        field: _canonical_value(provenance.get(field), column_name=field)
                        for field in _SUPPLEMENTAL_PROVENANCE_FIELDS
                    },
                },
                "history": history_tables,
            }
        )

    if not supplemental_imports:
        return None
    supplemental_imports.sort(key=sha256_json)
    return sha256_json(
        {
            "contract": "supplemental-terminal-history-attestation-v1",
            "generation_id": generation_text,
            "primary_import_run_id": primary_text,
            "imports": supplemental_imports,
        }
    )


def _effective_import_completion_digest(
    session: Session,
    *,
    candidate: rel.ReleaseCandidate,
    source_import_run_id: uuid.UUID,
) -> tuple[str, str]:
    primary_digest = _import_completion_digest(
        session,
        candidate=candidate,
        source_import_run_id=source_import_run_id,
    )
    supplemental_digest = _supplemental_terminal_history_digest(
        session,
        candidate=candidate,
        source_import_run_id=source_import_run_id,
    )
    if supplemental_digest is None:
        return primary_digest, BUILDER_CONTRACT_VERSION
    return (
        sha256_json(
            {
                "contract": "effective-import-completion-v2",
                "primary_import_completion_sha256": primary_digest,
                "supplemental_terminal_history_sha256": supplemental_digest,
            }
        ),
        SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT,
    )


def _typed_import_linkage_digest(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> str:
    evidence = _table_rows(
        session,
        table_name="source_import_entity_evidence",
        expected_columns=(
            "evidence_id",
            "import_batch_id",
            "entity_kind",
            "source_identity",
            "source_sha256",
            "target_entity_type",
            "target_entity_id",
            "provenance",
            "imported_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.import_batch_id, candidate.source_import_batch_id, dialect
        ),
    )
    links = _table_rows(
        session,
        table_name="source_import_native_links",
        expected_columns=(
            "link_id",
            "evidence_id",
            "import_batch_id",
            "import_run_id",
            "entity_kind",
            "project_id",
            "section_id",
            "task_id",
            "content_version_id",
            "request_tombstone_id",
            "linked_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.import_batch_id, candidate.source_import_batch_id, dialect
        ),
    )
    return sha256_json(
        {
            "contract": "typed-import-source-to-native-linkage-v1",
            "source_evidence": evidence,
            "native_links": links,
        }
    )


def _latest_reconciliation_run_id(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> uuid.UUID:
    run_id = session.scalar(
        select(tx.ProjectionReconciliationRun.reconciliation_run_id)
        .where(
            tx.ProjectionReconciliationRun.generation_id == candidate.generation_id,
            tx.ProjectionReconciliationRun.projection_epoch_id == candidate.projection_epoch_id,
            tx.ProjectionReconciliationRun.candidate_id == candidate.candidate_id,
        )
        .order_by(
            tx.ProjectionReconciliationRun.started_at.desc(),
            tx.ProjectionReconciliationRun.reconciliation_run_id.desc(),
        )
        .limit(1)
    )
    if run_id is None:
        raise ReleaseAuthorityError(
            "candidate manifest requires exact approval-time reconciliation evidence"
        )
    return run_id


def _reconciliation_evidence_digest(
    session: Session,
    *,
    candidate: rel.ReleaseCandidate,
    reconciliation_run_id: uuid.UUID,
) -> str:
    runs = _table_rows(
        session,
        table_name="projection_reconciliation_runs",
        expected_columns=(
            "reconciliation_run_id",
            "generation_id",
            "projection_epoch_id",
            "corpus_identity",
            "candidate_id",
            "registry_version_id",
            "observation_started_at",
            "observation_completed_at",
            "external_snapshot_identity",
            "external_high_water",
            "corpus_manifest_sha256",
            "scope_complete",
            "adapter_contract_version",
            "evidence_recorded_at",
            "status",
            "expected_items",
            "processed_items",
            "started_at",
            "completed_at",
        ),
        where=lambda table, dialect: (
            _uuid_match(table.c.reconciliation_run_id, reconciliation_run_id, dialect)
            & _uuid_match(table.c.generation_id, candidate.generation_id, dialect)
            & _uuid_match(table.c.projection_epoch_id, candidate.projection_epoch_id, dialect)
            & _uuid_match(table.c.candidate_id, candidate.candidate_id, dialect)
        ),
    )
    selected = _single_row(runs, label="approval reconciliation run")
    if selected is None:
        raise ReleaseAuthorityError(
            "candidate manifest approval reconciliation run is missing or no longer candidate-bound"
        )
    items = _table_rows(
        session,
        table_name="projection_reconciliation_items",
        expected_columns=(
            "reconciliation_item_id",
            "reconciliation_run_id",
            "item_identity",
            "entity_kind",
            "mapping_id",
            "outcome",
            "evidence",
            "recorded_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.reconciliation_run_id, reconciliation_run_id, dialect
        ),
    )
    return sha256_json(
        {
            "contract": "exact-approval-reconciliation-evidence-v2",
            "reconciliation_run_id": str(reconciliation_run_id),
            "run_schema": {
                "table": runs["table"],
                "present": runs["present"],
                "columns_present": runs["columns_present"],
            },
            "selected_run": selected,
            "items": items,
        }
    )


def _identity(
    session: Session,
    candidate: rel.ReleaseCandidate,
    *,
    approval_reconciliation_run_id: uuid.UUID | None = None,
) -> dict[str, object]:
    batch = session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
    active = session.get(models.ActiveSectionRegistry, candidate.generation_id)
    epoch = session.get(tx.ProjectionEpoch, candidate.projection_epoch_id)
    baseline = session.get(tx.ShadowBaseline, candidate.shadow_baseline_id)
    if batch is None or active is None or epoch is None or baseline is None:
        raise ReleaseAuthorityError("candidate manifest authority identity is incomplete")
    registry = session.get(models.SectionRegistryVersion, active.registry_version_id)
    if registry is None:
        raise ReleaseAuthorityError("candidate manifest active registry version is missing")
    if (
        batch.generation_id != candidate.generation_id
        or epoch.generation_id != candidate.generation_id
        or baseline.generation_id != candidate.generation_id
    ):
        raise ReleaseAuthorityError("candidate manifest authority generation mismatch")
    if (
        registry.generation_id != candidate.generation_id
        or registry.import_run_id != batch.import_run_id
    ):
        raise ReleaseAuthorityError("candidate manifest registry lineage mismatch")
    if approval_reconciliation_run_id is None:
        approval_reconciliation_run_id = _latest_reconciliation_run_id(
            session, candidate=candidate
        )
    import_completion_sha256, builder_contract_version = _effective_import_completion_digest(
        session,
        candidate=candidate,
        source_import_run_id=batch.import_run_id,
    )
    identity: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "candidate_id": str(candidate.candidate_id),
        "generation_id": str(candidate.generation_id),
        "source_import_batch_id": str(batch.import_batch_id),
        "source_import_run_id": str(batch.import_run_id),
        "shadow_baseline_id": str(baseline.shadow_baseline_id),
        "projection_epoch_id": str(epoch.projection_epoch_id),
        "registry_version_id": str(registry.registry_version_id),
        "honest_binding_id": str(registry.contract_binding_id),
        "approval_reconciliation_run_id": str(approval_reconciliation_run_id),
        "builder_contract_version": builder_contract_version,
        "mapping_membership_sha256": _mapping_membership_digest(
            session, candidate=candidate
        ),
        "import_completion_sha256": import_completion_sha256,
        "typed_import_linkage_sha256": _typed_import_linkage_digest(
            session, candidate=candidate
        ),
        "reconciliation_evidence_sha256": _reconciliation_evidence_digest(
            session,
            candidate=candidate,
            reconciliation_run_id=approval_reconciliation_run_id,
        ),
    }
    return identity


def build_candidate_manifest(
    session: Session,
    *,
    uuid_factory: Callable[[], uuid.UUID],
    candidate: rel.ReleaseCandidate,
    built_at: datetime,
) -> manifest_models.ReleaseCandidateManifest:
    existing = session.scalar(
        select(manifest_models.ReleaseCandidateManifest).where(
            manifest_models.ReleaseCandidateManifest.candidate_id
            == candidate.candidate_id
        )
    )
    if existing is not None and existing.manifest_version != MANIFEST_VERSION:
        raise ReleaseAuthorityError(
            "candidate already has a historical authority-manifest contract; "
            "create and approve a forward candidate instead of reinterpreting it"
        )
    identity = _identity(
        session,
        candidate,
        approval_reconciliation_run_id=(
            None if existing is None else existing.approval_reconciliation_run_id
        ),
    )
    fingerprint = sha256_json(identity)
    if existing is not None:
        if existing.canonical_fingerprint != fingerprint:
            raise ReleaseAuthorityError(
                "candidate authority manifest changed after creation"
            )
        return existing
    row = manifest_models.ReleaseCandidateManifest(
        manifest_id=uuid_factory(),
        candidate_id=candidate.candidate_id,
        manifest_version=MANIFEST_VERSION,
        canonical_fingerprint=fingerprint,
        generation_id=candidate.generation_id,
        source_import_batch_id=candidate.source_import_batch_id,
        source_import_run_id=uuid.UUID(str(identity["source_import_run_id"])),
        shadow_baseline_id=candidate.shadow_baseline_id,
        projection_epoch_id=candidate.projection_epoch_id,
        registry_version_id=uuid.UUID(str(identity["registry_version_id"])),
        honest_binding_id=uuid.UUID(str(identity["honest_binding_id"])),
        approval_reconciliation_run_id=uuid.UUID(
            str(identity["approval_reconciliation_run_id"])
        ),
        readiness_inventory_sha256=None,
        readiness_completion_sha256=None,
        builder_contract_version=str(identity["builder_contract_version"]),
        built_at=built_at,
        **{field: str(identity[field]) for field in COMPONENT_FIELDS},
    )
    session.add(row)
    session.flush()
    return row


def bind_approval_manifest(
    session: Session,
    *,
    uuid_factory: Callable[[], uuid.UUID],
    approval: rel.CutoverApproval,
    candidate: rel.ReleaseCandidate,
    bound_at: datetime,
) -> manifest_models.CutoverApprovalManifestBinding:
    manifest = build_candidate_manifest(
        session, uuid_factory=uuid_factory, candidate=candidate, built_at=bound_at
    )
    existing = session.scalar(
        select(manifest_models.CutoverApprovalManifestBinding).where(
            manifest_models.CutoverApprovalManifestBinding.approval_id
            == approval.approval_id
        )
    )
    if existing is not None:
        return existing
    row = manifest_models.CutoverApprovalManifestBinding(
        binding_id=uuid_factory(),
        approval_id=approval.approval_id,
        candidate_id=candidate.candidate_id,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        canonical_fingerprint=manifest.canonical_fingerprint,
        bound_at=bound_at,
    )
    session.add(row)
    session.flush()
    return row


def revalidate_candidate_manifest(
    session: Session,
    *,
    uuid_factory: Callable[[], uuid.UUID],
    candidate: rel.ReleaseCandidate,
    revalidated_at: datetime,
) -> manifest_models.CandidateManifestRevalidation:
    binding = session.scalar(
        select(manifest_models.CutoverApprovalManifestBinding).where(
            manifest_models.CutoverApprovalManifestBinding.candidate_id
            == candidate.candidate_id
        )
    )
    manifest = None if binding is None else session.get(
        manifest_models.ReleaseCandidateManifest, binding.manifest_id
    )
    if manifest is None:
        raise ReleaseAuthorityError("approved candidate lacks an authority manifest")
    if manifest.manifest_version != MANIFEST_VERSION:
        raise ReleaseAuthorityError(
            "approved candidate uses a historical authority-manifest contract; "
            "create and approve a forward candidate before activation"
        )
    if manifest.approval_reconciliation_run_id is None:
        raise ReleaseAuthorityError(
            "forward candidate manifest lacks exact approval-time reconciliation identity"
        )
    identity = _identity(
        session,
        candidate,
        approval_reconciliation_run_id=manifest.approval_reconciliation_run_id,
    )
    observed = sha256_json(identity)
    result = "matched" if observed == manifest.canonical_fingerprint else "stale"
    existing = session.scalar(
        select(manifest_models.CandidateManifestRevalidation).where(
            manifest_models.CandidateManifestRevalidation.candidate_id
            == candidate.candidate_id,
            manifest_models.CandidateManifestRevalidation.observed_fingerprint
            == observed,
            manifest_models.CandidateManifestRevalidation.revalidated_at
            == revalidated_at,
        )
    )
    if existing is not None:
        return existing
    row = manifest_models.CandidateManifestRevalidation(
        revalidation_id=uuid_factory(),
        candidate_id=candidate.candidate_id,
        manifest_id=manifest.manifest_id,
        manifest_version=manifest.manifest_version,
        approved_fingerprint=manifest.canonical_fingerprint,
        observed_fingerprint=observed,
        observed_readiness_inventory_sha256=None,
        observed_readiness_completion_sha256=None,
        result=result,
        revalidated_at=revalidated_at,
        **{
            f"observed_{field}": str(identity[field])
            for field in COMPONENT_FIELDS
        },
    )
    session.add(row)
    session.flush()
    return row
