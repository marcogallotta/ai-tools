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
from . import stage5_models as tx
from . import stage6_models as rel
from .release_evidence import ReleaseAuthorityError, sha256_json

MANIFEST_VERSION = 2
BUILDER_CONTRACT_VERSION = "candidate-authority-v2"

COMPONENT_FIELDS = (
    "mapping_membership_sha256",
    "import_completion_sha256",
    "typed_import_linkage_sha256",
    "reconciliation_evidence_sha256",
    "readiness_inventory_sha256",
    "readiness_completion_sha256",
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


def _reconciliation_evidence_digest(
    session: Session, *, candidate: rel.ReleaseCandidate
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
            _uuid_match(table.c.generation_id, candidate.generation_id, dialect)
            & _uuid_match(
                table.c.projection_epoch_id, candidate.projection_epoch_id, dialect
            )
        ),
    )
    selected: dict[str, Any] | None = None
    if runs["rows"]:
        selected = max(
            runs["rows"],
            key=lambda row: (
                str(row.get("started_at") or ""),
                str(row.get("reconciliation_run_id") or ""),
            ),
        )
    selected_run_id = (
        uuid.UUID(str(selected["reconciliation_run_id"])) if selected is not None else None
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
        where=(
            None
            if selected_run_id is None
            else lambda table, dialect: _uuid_match(
                table.c.reconciliation_run_id, selected_run_id, dialect
            )
        ),
    )
    if selected_run_id is None:
        items["rows"] = []
    return sha256_json(
        {
            "contract": "latest-candidate-reconciliation-evidence-v1",
            "selection": "max(started_at,reconciliation_run_id) for generation and epoch",
            "run_schema": {
                "table": runs["table"],
                "present": runs["present"],
                "columns_present": runs["columns_present"],
            },
            "selected_run": selected,
            "items": items,
        }
    )


def _readiness_inventory_digest(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> str:
    inventory = _table_rows(
        session,
        table_name="worker_probe_inventories",
        expected_columns=(
            "inventory_id",
            "candidate_id",
            "projection_epoch_id",
            "inventory_version",
            "required_probe_count",
            "inventory_sha256",
            "inventory_contract_version",
            "sealed_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.candidate_id, candidate.candidate_id, dialect
        ),
    )
    inventory_ids = [
        uuid.UUID(str(row["inventory_id"]))
        for row in inventory["rows"]
        if row.get("inventory_id") is not None
    ]
    requirements = _table_rows(
        session,
        table_name="worker_probe_requirements",
        expected_columns=(
            "requirement_id",
            "inventory_id",
            "probe_kind",
            "ordinal",
            "probe_contract_version",
        ),
        where=(
            None
            if not inventory_ids
            else lambda table, dialect: _uuid_membership(
                table.c.inventory_id, inventory_ids, dialect
            )
        ),
    )
    if not inventory_ids:
        requirements["rows"] = []
    return sha256_json(
        {
            "contract": "worker-readiness-inventory-v1",
            "inventory": inventory,
            "requirements": requirements,
        }
    )


def _readiness_completion_digest(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> str:
    readiness = _table_rows(
        session,
        table_name="projection_worker_readiness",
        expected_columns=(
            "readiness_id",
            "candidate_id",
            "projection_epoch_id",
            "reconciliation_run_id",
            "probe_inventory_id",
            "worker_identity",
            "worker_release",
            "payload",
            "readiness_sha256",
            "ready_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.candidate_id, candidate.candidate_id, dialect
        ),
    )
    evidence = _table_rows(
        session,
        table_name="worker_probe_evidence",
        expected_columns=(
            "evidence_id",
            "readiness_id",
            "requirement_id",
            "inventory_id",
            "candidate_id",
            "projection_epoch_id",
            "probe_kind",
            "execution_identity",
            "worker_identity",
            "deployed_artifact_sha256",
            "result",
            "observed_at",
            "evidence_artifact_identity",
            "evidence_sha256",
            "recorded_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.candidate_id, candidate.candidate_id, dialect
        ),
    )
    completion = _table_rows(
        session,
        table_name="worker_readiness_completions",
        expected_columns=(
            "completion_id",
            "readiness_id",
            "inventory_id",
            "candidate_id",
            "projection_epoch_id",
            "completion_state",
            "required_probe_count",
            "passed_probe_count",
            "completion_sha256",
            "completed_at",
        ),
        where=lambda table, dialect: _uuid_match(
            table.c.candidate_id, candidate.candidate_id, dialect
        ),
    )
    return sha256_json(
        {
            "contract": "worker-readiness-execution-completion-v1",
            "readiness": readiness,
            "probe_evidence": evidence,
            "completion": completion,
        }
    )


def _identity(session: Session, candidate: rel.ReleaseCandidate) -> dict[str, object]:
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
        "builder_contract_version": BUILDER_CONTRACT_VERSION,
        "mapping_membership_sha256": _mapping_membership_digest(
            session, candidate=candidate
        ),
        "import_completion_sha256": _import_completion_digest(
            session,
            candidate=candidate,
            source_import_run_id=batch.import_run_id,
        ),
        "typed_import_linkage_sha256": _typed_import_linkage_digest(
            session, candidate=candidate
        ),
        "reconciliation_evidence_sha256": _reconciliation_evidence_digest(
            session, candidate=candidate
        ),
        "readiness_inventory_sha256": _readiness_inventory_digest(
            session, candidate=candidate
        ),
        "readiness_completion_sha256": _readiness_completion_digest(
            session, candidate=candidate
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
    identity = _identity(session, candidate)
    fingerprint = sha256_json(identity)
    existing = session.scalar(
        select(manifest_models.ReleaseCandidateManifest).where(
            manifest_models.ReleaseCandidateManifest.candidate_id
            == candidate.candidate_id
        )
    )
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
        builder_contract_version=BUILDER_CONTRACT_VERSION,
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
    manifest = session.scalar(
        select(manifest_models.ReleaseCandidateManifest).where(
            manifest_models.ReleaseCandidateManifest.candidate_id
            == candidate.candidate_id
        )
    )
    if manifest is None:
        raise ReleaseAuthorityError("approved candidate lacks an authority manifest")
    identity = _identity(session, candidate)
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
