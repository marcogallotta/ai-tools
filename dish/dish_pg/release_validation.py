"""Application-level validation for Agent A release/cutover evidence contracts."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Collection

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import artifact_identity_models as artifact
from . import import_link_models as import_links
from . import legacy_request_models as legacy
from . import models
from . import readiness_evidence_models as readiness
from . import stage5_models as tx
from . import stage6_models as rel
from .cutover_chronology import _utc_comparable
from .release_evidence import ReleaseAuthorityError, sha256_json

RECONCILIATION_FRESHNESS_WINDOW = timedelta(hours=1)
SUPPORTED_RECONCILIATION_ADAPTERS = {
    "asana-snapshot-v1": "snapshot",
    "asana-high-water-v1": "high_water",
}

_TARGET_TYPE = {
    "project": "governed_project",
    "section": "governed_section",
    "task": "dish_task",
    "content": "task_content_version",
    "request_tombstone": "legacy_request_tombstone",
}
_TARGET_ATTR = {
    "project": "project_id",
    "section": "section_id",
    "task": "task_id",
    "content": "content_version_id",
    "request_tombstone": "request_tombstone_id",
}


def active_mapping_membership(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> set[tuple[str, uuid.UUID]]:
    result: set[tuple[str, uuid.UUID]] = set()
    for kind, model in (
        ("project", tx.ProjectProjectionMapping),
        ("section", tx.SectionProjectionMapping),
        ("task", tx.TaskProjectionMapping),
    ):
        ids = session.scalars(
            select(model.mapping_id).where(
                model.generation_id == candidate.generation_id,
                model.projection_epoch_id == candidate.projection_epoch_id,
                model.state == "active",
            )
        ).all()
        result.update((kind, value) for value in ids)
    return result


def reconciliation_corpus_sha256(
    *, candidate: rel.ReleaseCandidate, membership: set[tuple[str, uuid.UUID]]
) -> str:
    return sha256_json(
        {
            "contract": "release-active-projection-corpus-v1",
            "candidate_id": str(candidate.candidate_id),
            "generation_id": str(candidate.generation_id),
            "projection_epoch_id": str(candidate.projection_epoch_id),
            "items": [
                {"entity_kind": kind, "mapping_id": str(mapping_id)}
                for kind, mapping_id in sorted(membership, key=lambda item: (item[0], str(item[1])))
            ],
        }
    )


@dataclass(frozen=True)
class ReconciliationValidation:
    passed: bool
    details: dict[str, Any]
    run: tx.ProjectionReconciliationRun | None


def validate_reconciliation(
    session: Session,
    *,
    candidate: rel.ReleaseCandidate,
    as_of: datetime,
) -> ReconciliationValidation:
    membership = active_mapping_membership(session, candidate=candidate)
    active_registry = session.get(models.ActiveSectionRegistry, candidate.generation_id)
    run = session.scalar(
        select(tx.ProjectionReconciliationRun)
        .where(
            tx.ProjectionReconciliationRun.generation_id == candidate.generation_id,
            tx.ProjectionReconciliationRun.projection_epoch_id == candidate.projection_epoch_id,
        )
        .order_by(
            tx.ProjectionReconciliationRun.started_at.desc(),
            tx.ProjectionReconciliationRun.reconciliation_run_id.desc(),
        )
        .limit(1)
    )
    required = membership
    actual: set[tuple[str, uuid.UUID]] = set()
    invalid: list[str] = []
    if run is not None:
        for item in session.scalars(
            select(tx.ProjectionReconciliationItem).where(
                tx.ProjectionReconciliationItem.reconciliation_run_id == run.reconciliation_run_id
            )
        ):
            identity = (
                item.entity_kind,
                item.mapping_id,
            )
            if (
                item.mapping_id is None
                or item.entity_kind not in {"project", "section", "task"}
                or item.outcome not in {"matched", "reprojected"}
            ):
                invalid.append(item.item_identity)
                continue
            pair = (item.entity_kind, item.mapping_id)
            actual.add(pair)
            if pair not in required:
                invalid.append(item.item_identity)

    missing = required - actual
    extra = actual - required
    expected_digest = reconciliation_corpus_sha256(candidate=candidate, membership=required)
    boundary_ok = False
    fresh = False
    if run is not None and run.observation_completed_at is not None:
        age = _utc_comparable(as_of) - _utc_comparable(run.observation_completed_at)
        fresh = timedelta(0) <= age <= RECONCILIATION_FRESHNESS_WINDOW
        adapter_boundary = SUPPORTED_RECONCILIATION_ADAPTERS.get(
            str(run.adapter_contract_version)
        )
        boundary_ok = (
            (adapter_boundary == "snapshot" and bool(run.external_snapshot_identity) and not run.external_high_water)
            or (adapter_boundary == "high_water" and bool(run.external_high_water) and not run.external_snapshot_identity)
        )
    passed = bool(
        required
        and run is not None
        and run.candidate_id == candidate.candidate_id
        and active_registry is not None
        and run.registry_version_id == active_registry.registry_version_id
        and run.status == "complete"
        and run.scope_complete is True
        and run.expected_items == len(required)
        and run.processed_items == len(required)
        and not missing
        and not extra
        and not invalid
        and run.corpus_manifest_sha256 == expected_digest
        and run.observation_started_at is not None
        and run.observation_completed_at is not None
        and run.evidence_recorded_at is not None
        and _utc_comparable(run.observation_completed_at)
        >= _utc_comparable(run.observation_started_at)
        and _utc_comparable(run.evidence_recorded_at)
        >= _utc_comparable(run.observation_completed_at)
        and boundary_ok
        and fresh
    )
    by_kind = {
        kind: sorted(str(mapping_id) for item_kind, mapping_id in required if item_kind == kind)
        for kind in ("project", "section", "task")
    }
    details: dict[str, Any] = {
        "active_mappings": len(required),
        "reconciled_mappings": len(actual),
        "required_project_mapping_ids": by_kind["project"],
        "required_section_mapping_ids": by_kind["section"],
        "required_task_mapping_ids": by_kind["task"],
        "missing_mapping_membership": [f"{kind}:{value}" for kind, value in sorted(missing, key=lambda item: (item[0], str(item[1])))],
        "extra_mapping_membership": [f"{kind}:{value}" for kind, value in sorted(extra, key=lambda item: (item[0], str(item[1])))],
        "invalid_reconciliation_rows": len(invalid),
        "reconciliation_expected": None if run is None else run.expected_items,
        "reconciliation_processed": None if run is None else run.processed_items,
        "reconciliation_status": None if run is None else run.status,
        "candidate_bound": run is not None and run.candidate_id == candidate.candidate_id,
        "registry_bound": run is not None and active_registry is not None and run.registry_version_id == active_registry.registry_version_id,
        "scope_complete": None if run is None else run.scope_complete,
        "corpus_manifest_expected": expected_digest,
        "corpus_manifest_observed": None if run is None else run.corpus_manifest_sha256,
        "adapter_contract_version": None if run is None else run.adapter_contract_version,
        "external_boundary_supported": boundary_ok,
        "fresh": fresh,
    }
    return ReconciliationValidation(passed, details, run)


def _required_import_targets(
    session: Session, *, import_run_id: uuid.UUID
) -> set[tuple[str, uuid.UUID]]:
    required: set[tuple[str, uuid.UUID]] = set()
    required.update(
        ("project", value)
        for value in session.scalars(
            select(models.GovernedProject.project_id).where(
                models.GovernedProject.import_run_id == import_run_id
            )
        )
    )
    required.update(
        ("section", value)
        for value in session.scalars(
            select(models.GovernedSection.section_id).where(
                models.GovernedSection.import_run_id == import_run_id
            )
        )
    )
    required.update(
        ("task", value)
        for value in session.scalars(
            select(models.DishTask.task_id).where(
                models.DishTask.import_run_id == import_run_id,
                models.DishTask.creation_route == "import",
            )
        )
    )
    required.update(
        ("content", value)
        for value in session.scalars(
            select(models.ContentVersion.content_version_id).where(
                models.ContentVersion.import_run_id == import_run_id,
                models.ContentVersion.creator_route == "import",
            )
        )
    )
    required.update(
        ("request_tombstone", value)
        for value in session.scalars(
            select(legacy.LegacyRequestTombstone.tombstone_id).where(
                legacy.LegacyRequestTombstone.import_run_id == import_run_id
            )
        )
    )
    return required


def validate_typed_import_linkage(
    session: Session, *, candidate: rel.ReleaseCandidate
) -> tuple[bool, dict[str, Any]]:
    batch = session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
    if batch is None:
        return False, {"reason": "missing source import batch"}
    required = _required_import_targets(session, import_run_id=batch.import_run_id)
    evidence_rows = session.scalars(
        select(tx.SourceImportEntityEvidence).where(
            tx.SourceImportEntityEvidence.import_batch_id == batch.import_batch_id
        )
    ).all()
    evidence_targets: dict[uuid.UUID, tuple[str, uuid.UUID]] = {}
    invalid_evidence: list[str] = []
    for row in evidence_rows:
        target = (row.entity_kind, row.target_entity_id)
        if _TARGET_TYPE.get(row.entity_kind) != row.target_entity_type:
            invalid_evidence.append(str(row.evidence_id))
        evidence_targets[row.evidence_id] = target
    links = session.scalars(
        select(import_links.SourceImportNativeLink).where(
            import_links.SourceImportNativeLink.import_batch_id == batch.import_batch_id
        )
    ).all()
    linked_targets: set[tuple[str, uuid.UUID]] = set()
    linked_evidence: set[uuid.UUID] = set()
    invalid_links: list[str] = []
    for link in links:
        target_id = getattr(link, _TARGET_ATTR[link.entity_kind])
        target = (link.entity_kind, target_id)
        linked_targets.add(target)
        linked_evidence.add(link.evidence_id)
        if (
            link.import_run_id != batch.import_run_id
            or evidence_targets.get(link.evidence_id) != target
        ):
            invalid_links.append(str(link.link_id))
    evidence_target_set = set(evidence_targets.values())
    passed = bool(
        required
        and required == evidence_target_set == linked_targets
        and set(evidence_targets) == linked_evidence
        and not invalid_evidence
        and not invalid_links
        and batch.expected_entities == len(required)
        and batch.imported_entities == len(required)
    )
    return passed, {
        "required_native_objects": len(required),
        "source_evidence_objects": len(evidence_target_set),
        "typed_linked_objects": len(linked_targets),
        "missing_source_evidence": [f"{k}:{v}" for k, v in sorted(required - evidence_target_set, key=lambda x: (x[0], str(x[1])))],
        "extra_source_evidence": [f"{k}:{v}" for k, v in sorted(evidence_target_set - required, key=lambda x: (x[0], str(x[1])))],
        "missing_typed_links": [f"{k}:{v}" for k, v in sorted(required - linked_targets, key=lambda x: (x[0], str(x[1])))],
        "extra_typed_links": [f"{k}:{v}" for k, v in sorted(linked_targets - required, key=lambda x: (x[0], str(x[1])))],
        "unlinked_evidence_ids": sorted(str(value) for value in set(evidence_targets) - linked_evidence),
        "invalid_evidence_ids": invalid_evidence,
        "invalid_link_ids": invalid_links,
    }


def validate_writer_fence_observation(
    session: Session,
    *,
    fence: rel.LegacyWriterFence,
    required_writer_inventory: Collection[str] | None,
) -> artifact.WriterFenceArtifactObservation:
    if fence.artifact_observation_id is None:
        raise ReleaseAuthorityError("writer fence lacks the artifact observation bound to engagement")
    observation = session.get(
        artifact.WriterFenceArtifactObservation, fence.artifact_observation_id
    )
    if observation is None:
        raise ReleaseAuthorityError("writer fence bound artifact observation is missing")
    payload = {
        "fence_id": str(fence.fence_id),
        "candidate_id": str(fence.candidate_id),
        "artifact_generation_identity": observation.artifact_generation_identity,
        "canonical_path": observation.canonical_path,
        "content_sha256": observation.content_sha256,
        "filesystem_device": observation.filesystem_device,
        "filesystem_inode": observation.filesystem_inode,
        "file_type": "regular",
        "regular_file": True,
        "verification_result": "matched",
        "observation_contract_version": observation.observation_contract_version,
        "observed_at": (
            observation.observed_at
            if observation.observed_at.tzinfo is not None
            else observation.observed_at.replace(tzinfo=timezone.utc)
        ).isoformat(),
    }
    if (
        observation.fence_id != fence.fence_id
        or observation.candidate_id != fence.candidate_id
        or observation.file_type != "regular"
        or observation.regular_file is not True
        or observation.verification_result != "matched"
        or fence.artifact_verification_result != "matched"
        or observation.evidence_sha256 != sha256_json(payload)
    ):
        raise ReleaseAuthorityError("writer fence engagement is not bound to an exact persisted matched observation")

    if required_writer_inventory is None:
        raise ReleaseAuthorityError(
            "required legacy writer inventory is not configured; "
            "TODO: supply Marco's local enumeration of every real legacy writer path"
        )
    required = {
        value.strip()
        for value in required_writer_inventory
        if isinstance(value, str) and value.strip()
    }
    if not required or len(required) != len(required_writer_inventory):
        raise ReleaseAuthorityError(
            "required legacy writer inventory must be a non-empty set of nonblank identities"
        )
    actual = set(
        session.scalars(
            select(rel.LegacyWriterFence.target_identity).where(
                rel.LegacyWriterFence.candidate_id == fence.candidate_id,
                rel.LegacyWriterFence.state.in_(("engaged", "verified")),
            )
        ).all()
    )
    missing = required - actual
    extra = actual - required
    if missing or extra:
        raise ReleaseAuthorityError(
            "legacy writer fence inventory mismatch: "
            f"missing_writer_targets={sorted(missing)!r}; "
            f"extra_writer_targets={sorted(extra)!r}"
        )
    return observation



def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def worker_inventory_sha256(
    *,
    candidate_id: uuid.UUID,
    projection_epoch_id: uuid.UUID,
    inventory_version: int,
    inventory_contract_version: str,
    requirements: list[readiness.WorkerProbeRequirement],
) -> str:
    return sha256_json({
        "contract": "worker-probe-inventory-v1",
        "candidate_id": str(candidate_id),
        "projection_epoch_id": str(projection_epoch_id),
        "inventory_version": inventory_version,
        "inventory_contract_version": inventory_contract_version,
        "requirements": [
            {
                "probe_kind": item.probe_kind,
                "ordinal": item.ordinal,
                "probe_contract_version": item.probe_contract_version,
            }
            for item in sorted(requirements, key=lambda row: row.ordinal)
        ],
    })


def worker_probe_evidence_sha256(
    *,
    readiness_id: uuid.UUID,
    requirement_id: uuid.UUID,
    inventory_id: uuid.UUID,
    candidate_id: uuid.UUID,
    projection_epoch_id: uuid.UUID,
    probe_kind: str,
    execution_identity: str,
    worker_identity: str,
    deployed_artifact_sha256: str,
    result: str,
    observed_at: datetime,
    evidence_artifact_identity: str,
) -> str:
    return sha256_json({
        "contract": "worker-probe-evidence-v1",
        "readiness_id": str(readiness_id),
        "requirement_id": str(requirement_id),
        "inventory_id": str(inventory_id),
        "candidate_id": str(candidate_id),
        "projection_epoch_id": str(projection_epoch_id),
        "probe_kind": probe_kind,
        "execution_identity": execution_identity,
        "worker_identity": worker_identity,
        "deployed_artifact_sha256": deployed_artifact_sha256,
        "result": result,
        "observed_at": _iso_utc(observed_at),
        "evidence_artifact_identity": evidence_artifact_identity,
    })


def worker_completion_sha256(
    *,
    readiness_id: uuid.UUID,
    inventory_id: uuid.UUID,
    candidate_id: uuid.UUID,
    projection_epoch_id: uuid.UUID,
    required_probe_count: int,
    passed_probe_count: int,
    completed_at: datetime,
) -> str:
    return sha256_json({
        "contract": "worker-readiness-completion-v1",
        "readiness_id": str(readiness_id),
        "inventory_id": str(inventory_id),
        "candidate_id": str(candidate_id),
        "projection_epoch_id": str(projection_epoch_id),
        "completion_state": "complete",
        "required_probe_count": required_probe_count,
        "passed_probe_count": passed_probe_count,
        "completed_at": _iso_utc(completed_at),
    })

def validate_worker_readiness(
    session: Session,
    *,
    candidate: rel.ReleaseCandidate,
    row: rel.ProjectionWorkerReadiness,
    deployed_artifact_sha256: str,
    as_of: datetime,
) -> dict[str, Any]:
    inventory = session.get(readiness.WorkerProbeInventory, row.probe_inventory_id)
    if inventory is None:
        raise ReleaseAuthorityError("worker readiness lacks a sealed typed inventory")
    requirements = session.scalars(
        select(readiness.WorkerProbeRequirement)
        .where(readiness.WorkerProbeRequirement.inventory_id == inventory.inventory_id)
        .order_by(readiness.WorkerProbeRequirement.ordinal)
    ).all()
    evidence = session.scalars(
        select(readiness.WorkerProbeEvidence).where(
            readiness.WorkerProbeEvidence.readiness_id == row.readiness_id
        )
    ).all()
    completion = session.scalar(
        select(readiness.WorkerReadinessCompletion).where(
            readiness.WorkerReadinessCompletion.readiness_id == row.readiness_id
        )
    )
    requirement_by_id = {item.requirement_id: item for item in requirements}
    exact_evidence = {
        item.requirement_id: item
        for item in evidence
        if item.requirement_id in requirement_by_id
        and item.inventory_id == inventory.inventory_id
        and item.candidate_id == candidate.candidate_id
        and item.projection_epoch_id == candidate.projection_epoch_id
        and item.probe_kind == requirement_by_id[item.requirement_id].probe_kind
        and item.worker_identity == row.worker_identity
        and item.deployed_artifact_sha256 == deployed_artifact_sha256
        and item.result == "pass"
        and item.evidence_sha256 == worker_probe_evidence_sha256(
            readiness_id=item.readiness_id,
            requirement_id=item.requirement_id,
            inventory_id=item.inventory_id,
            candidate_id=item.candidate_id,
            projection_epoch_id=item.projection_epoch_id,
            probe_kind=item.probe_kind,
            execution_identity=item.execution_identity,
            worker_identity=item.worker_identity,
            deployed_artifact_sha256=item.deployed_artifact_sha256,
            result=item.result,
            observed_at=item.observed_at,
            evidence_artifact_identity=item.evidence_artifact_identity,
        )
        and _utc_comparable(item.recorded_at) >= _utc_comparable(item.observed_at)
        and _utc_comparable(item.recorded_at) <= _utc_comparable(as_of)
    }
    if (
        inventory.candidate_id != candidate.candidate_id
        or inventory.projection_epoch_id != candidate.projection_epoch_id
        or inventory.required_probe_count <= 0
        or inventory.inventory_sha256 != worker_inventory_sha256(
            candidate_id=inventory.candidate_id,
            projection_epoch_id=inventory.projection_epoch_id,
            inventory_version=inventory.inventory_version,
            inventory_contract_version=inventory.inventory_contract_version,
            requirements=requirements,
        )
        or len(requirements) != inventory.required_probe_count
        or len({item.probe_kind for item in requirements}) != len(requirements)
        or len({item.ordinal for item in requirements}) != len(requirements)
        or set(exact_evidence) != set(requirement_by_id)
        or completion is None
        or completion.inventory_id != inventory.inventory_id
        or completion.candidate_id != candidate.candidate_id
        or completion.projection_epoch_id != candidate.projection_epoch_id
        or completion.completion_state != "complete"
        or completion.required_probe_count != inventory.required_probe_count
        or completion.passed_probe_count != inventory.required_probe_count
        or completion.completion_sha256 != worker_completion_sha256(
            readiness_id=completion.readiness_id,
            inventory_id=completion.inventory_id,
            candidate_id=completion.candidate_id,
            projection_epoch_id=completion.projection_epoch_id,
            required_probe_count=completion.required_probe_count,
            passed_probe_count=completion.passed_probe_count,
            completed_at=completion.completed_at,
        )
        or _utc_comparable(completion.completed_at) > _utc_comparable(as_of)
    ):
        raise ReleaseAuthorityError("projection worker readiness lacks exact completed typed probe evidence")
    return {
        "inventory_id": str(inventory.inventory_id),
        "required_probe_count": inventory.required_probe_count,
        "passed_probe_count": len(exact_evidence),
        "completion_id": str(completion.completion_id),
    }
