"""Externally controlled authority promotion after PostgreSQL restore or PITR.

The database may contain a complete historical authority timeline after recovery,
but restored rows do not make restored actors current.  This module consumes one
operator-issued control receipt, creates a new destructive-restore generation,
clones only the governed registry, and starts a disabled projection epoch.  A
separate, explicit rehydration transition may then reissue only current task
authority from the exact predecessor; transient workflow and external-effect state
remain forensic predecessor evidence and are never copied forward.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import candidate_manifest_models as manifest_models
from . import models
from . import stage3_models as wf
from . import stage5_models as projection_models
from . import stage6_models as release_models
from .recovery_rehydration import RECOVERY_REHYDRATION_REVISION, RecoveryRehydrationResult
from .release import ALEMBIC_HEAD
from .release_evidence import sha256_json
from .repositories import AuthorityRepository, RegistryRepository
from .transition import ProjectionService


class RestoreControlError(ValueError):
    """The external restore-control receipt is absent, stale, or inconsistent."""


def migration_revision_sha256(revision: str) -> str:
    """Hash the exact checked-in Alembic revision used as recovery provenance."""
    migration = Path(__file__).resolve().parent / "migrations" / "versions" / f"{revision}.py"
    try:
        content = migration.read_bytes()
    except OSError as exc:
        raise RestoreControlError(f"migration provenance unavailable for {revision}") from exc
    return hashlib.sha256(content).hexdigest()


def _parse_aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RestoreControlError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RestoreControlError(f"{field} must include an offset")
    return parsed


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestoreControlError(f"{field} must be a nonblank string")
    return value.strip()


def _lsn(value: object, field: str) -> str:
    lsn = _nonblank(value, field)
    if not re.fullmatch(r"[0-9A-F]+/[0-9A-F]{1,8}", lsn):
        raise RestoreControlError(f"{field} must be a canonical PostgreSQL LSN")
    return lsn


def _utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_instant(left: datetime, right: datetime) -> bool:
    return _utc_instant(left) == _utc_instant(right)


def _at_or_before(left: datetime, right: datetime) -> bool:
    return _utc_instant(left) <= _utc_instant(right)


@dataclass(frozen=True)
class RecoveredPhysicalState:
    database_name: str
    system_identifier: str
    schema_head: str
    backup_manifest_sha256: str
    backup_evidence_sha256: str
    recovery_timeline_id: int
    recovery_target_type: str
    recovery_target_lsn: str
    recovery_completion_lsn: str
    recovery_target_instance_sha256: str

    def evidence_payload(self) -> dict[str, object]:
        return {
            "database_name": self.database_name,
            "system_identifier": self.system_identifier,
            "schema_head": self.schema_head,
            "backup_manifest_sha256": self.backup_manifest_sha256,
            "backup_evidence_sha256": self.backup_evidence_sha256,
            "recovery_timeline_id": self.recovery_timeline_id,
            "recovery_target_type": self.recovery_target_type,
            "recovery_target_lsn": self.recovery_target_lsn,
            "recovery_completion_lsn": self.recovery_completion_lsn,
            "recovery_target_instance_sha256": self.recovery_target_instance_sha256,
        }

    @property
    def evidence_sha256(self) -> str:
        return sha256_json(self.evidence_payload())


@dataclass(frozen=True)
class RestoreControl:
    external_control_id: str
    predecessor_generation_id: uuid.UUID
    generation_id: uuid.UUID
    bootstrap_id: uuid.UUID
    bootstrap_capability_digest: bytes
    expected_database_name: str
    expected_system_identifier: str
    schema_head: str
    dish_release: str
    honest_release: str
    protocol_release: str
    openapi_release: str
    routing_release: str
    backup_manifest_sha256: str
    backup_evidence_sha256: str
    recovery_timeline_id: int
    recovery_target_type: str
    recovery_target_lsn: str
    recovery_completion_lsn: str
    recovery_target_instance_sha256: str
    recovery_evidence_sha256: str
    issued_at: datetime

    def as_json(self) -> dict[str, object]:
        value = asdict(self)
        value["predecessor_generation_id"] = str(self.predecessor_generation_id)
        value["generation_id"] = str(self.generation_id)
        value["bootstrap_id"] = str(self.bootstrap_id)
        del value["bootstrap_capability_digest"]
        value["bootstrap_capability_present"] = True
        value["issued_at"] = self.issued_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return value


@dataclass(frozen=True)
class RestorePromotionResult:
    predecessor_generation_id: uuid.UUID
    generation_id: uuid.UUID
    bootstrap_id: uuid.UUID
    registry_version_id: uuid.UUID
    registry_activation_id: uuid.UUID
    projection_epoch_id: uuid.UUID
    migration_event_id: uuid.UUID
    external_control_id: str
    promoted_at: datetime

    def as_json(self) -> dict[str, str]:
        return {
            key: (
                value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if isinstance(value, datetime)
                else str(value)
            )
            for key, value in asdict(self).items()
        }


def load_restore_control(path: Path) -> RestoreControl:
    """Load one exact operator receipt; absence or extra ambiguity fails closed."""
    source = path.expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RestoreControlError(f"external restore control unavailable: {source}") from exc
    except json.JSONDecodeError as exc:
        raise RestoreControlError("external restore control is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise RestoreControlError("external restore control must be a JSON object")
    required = {
        "external_control_id",
        "predecessor_generation_id",
        "generation_id",
        "bootstrap_id",
        "bootstrap_capability_sha256",
        "expected_database_name",
        "expected_system_identifier",
        "schema_head",
        "dish_release",
        "honest_release",
        "protocol_release",
        "openapi_release",
        "routing_release",
        "backup_manifest_sha256",
        "backup_evidence_sha256",
        "recovery_timeline_id",
        "recovery_target_type",
        "recovery_target_lsn",
        "recovery_completion_lsn",
        "recovery_target_instance_sha256",
        "recovery_evidence_sha256",
        "issued_at",
    }
    unknown = set(payload) - required
    missing = required - set(payload)
    if missing or unknown:
        raise RestoreControlError(
            "external restore control fields mismatch; "
            f"missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    try:
        digest = bytes.fromhex(
            _nonblank(
                payload["bootstrap_capability_sha256"],
                "bootstrap_capability_sha256",
            )
        )
    except ValueError as exc:
        raise RestoreControlError("bootstrap_capability_sha256 must be hexadecimal") from exc
    if len(digest) != 32:
        raise RestoreControlError("bootstrap_capability_sha256 must be an exact SHA-256 digest")
    try:
        predecessor = uuid.UUID(
            _nonblank(
                payload["predecessor_generation_id"],
                "predecessor_generation_id",
            )
        )
        generation = uuid.UUID(_nonblank(payload["generation_id"], "generation_id"))
        bootstrap = uuid.UUID(_nonblank(payload["bootstrap_id"], "bootstrap_id"))
    except ValueError as exc:
        raise RestoreControlError(
            "restore-control generation and bootstrap IDs must be UUIDs"
        ) from exc
    if predecessor == generation:
        raise RestoreControlError("restore generation must differ from its predecessor")
    digest_fields = (
        "backup_manifest_sha256",
        "backup_evidence_sha256",
        "recovery_target_instance_sha256",
        "recovery_evidence_sha256",
    )
    digests = {field: _nonblank(payload[field], field) for field in digest_fields}
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in digests.values()):
        raise RestoreControlError("physical recovery evidence digests must be lowercase SHA-256")
    timeline = payload["recovery_timeline_id"]
    if not isinstance(timeline, int) or isinstance(timeline, bool) or timeline <= 0:
        raise RestoreControlError("recovery_timeline_id must be a positive integer")
    target_type = _nonblank(payload["recovery_target_type"], "recovery_target_type")
    if target_type not in {"backup_end", "lsn"}:
        raise RestoreControlError("recovery_target_type must be backup_end or lsn")
    return RestoreControl(
        external_control_id=_nonblank(payload["external_control_id"], "external_control_id"),
        predecessor_generation_id=predecessor,
        generation_id=generation,
        bootstrap_id=bootstrap,
        bootstrap_capability_digest=digest,
        expected_database_name=_nonblank(
            payload["expected_database_name"], "expected_database_name"
        ),
        expected_system_identifier=_nonblank(
            payload["expected_system_identifier"], "expected_system_identifier"
        ),
        schema_head=_nonblank(payload["schema_head"], "schema_head"),
        dish_release=_nonblank(payload["dish_release"], "dish_release"),
        honest_release=_nonblank(payload["honest_release"], "honest_release"),
        protocol_release=_nonblank(payload["protocol_release"], "protocol_release"),
        openapi_release=_nonblank(payload["openapi_release"], "openapi_release"),
        routing_release=_nonblank(payload["routing_release"], "routing_release"),
        backup_manifest_sha256=digests["backup_manifest_sha256"],
        backup_evidence_sha256=digests["backup_evidence_sha256"],
        recovery_timeline_id=timeline,
        recovery_target_type=target_type,
        recovery_target_lsn=_lsn(payload["recovery_target_lsn"], "recovery_target_lsn"),
        recovery_completion_lsn=_lsn(
            payload["recovery_completion_lsn"], "recovery_completion_lsn"
        ),
        recovery_target_instance_sha256=digests[
            "recovery_target_instance_sha256"
        ],
        recovery_evidence_sha256=digests["recovery_evidence_sha256"],
        issued_at=_parse_aware(_nonblank(payload["issued_at"], "issued_at"), "issued_at"),
    )


def _clone_registry(
    session: Session,
    *,
    predecessor_generation_id: uuid.UUID,
    generation_id: uuid.UUID,
    at: datetime,
    uuid_factory: Callable[[], uuid.UUID],
) -> tuple[uuid.UUID, uuid.UUID]:
    current = session.get(models.ActiveSectionRegistry, predecessor_generation_id)
    if current is None:
        raise RestoreControlError("restored predecessor has no active governed registry")
    source = session.get(models.SectionRegistryVersion, current.registry_version_id)
    if source is None or source.generation_id != predecessor_generation_id:
        raise RestoreControlError("restored predecessor registry provenance is inconsistent")
    entries = session.scalars(
        select(models.SectionRegistryEntry)
        .where(models.SectionRegistryEntry.registry_version_id == source.registry_version_id)
        .order_by(models.SectionRegistryEntry.ordinal)
    ).all()
    if not entries:
        raise RestoreControlError("restored predecessor registry is empty")
    version_id = uuid_factory()
    activation_id = uuid_factory()
    repo = RegistryRepository(session)
    repo.add_registry_version(
        models.SectionRegistryVersion(
            registry_version_id=version_id,
            generation_id=generation_id,
            version_number=1,
            import_run_id=source.import_run_id,
            contract_binding_id=source.contract_binding_id,
            registry_sha256=source.registry_sha256,
            created_at=at,
        ),
        [
            models.SectionRegistryEntry(
                registry_version_id=version_id,
                section_id=entry.section_id,
                ordinal=entry.ordinal,
                display_name=entry.display_name,
                workflow_role=entry.workflow_role,
            )
            for entry in entries
        ],
    )
    repo.activate_registry(
        activation=models.SectionRegistryActivation(
            registry_activation_id=activation_id,
            generation_id=generation_id,
            registry_version_id=version_id,
            activation_route="import",
            import_run_id=source.import_run_id,
            command_execution_id=None,
            registry_revision=1,
            activated_at=at,
        ),
        current=models.ActiveSectionRegistry(
            generation_id=generation_id,
            registry_version_id=version_id,
            registry_activation_id=activation_id,
            registry_revision=1,
            updated_at=at,
        ),
    )
    return version_id, activation_id


def _authorized_release_candidate(
    session: Session,
    *,
    active: models.AuthorityGeneration,
    control: RestoreControl,
) -> release_models.ReleaseCandidate:
    """Prove the immutable historical authority transition used by recovery."""
    candidates = session.scalars(
        select(release_models.ReleaseCandidate).where(
            release_models.ReleaseCandidate.generation_id == active.generation_id,
            release_models.ReleaseCandidate.schema_head == control.schema_head,
            release_models.ReleaseCandidate.dish_release == control.dish_release,
            release_models.ReleaseCandidate.honest_release == control.honest_release,
            release_models.ReleaseCandidate.protocol_release == control.protocol_release,
            release_models.ReleaseCandidate.openapi_release == control.openapi_release,
            release_models.ReleaseCandidate.routing_release == control.routing_release,
        )
    ).all()
    if len(candidates) != 1:
        raise RestoreControlError(
            "recovered release authority does not identify exactly one candidate"
        )
    candidate = candidates[0]
    if candidate.status != "activated":
        raise RestoreControlError(
            f"recovered release candidate state is not rollback-burned: {candidate.status}"
        )
    if (
        candidate.validation_bundle_sha256 is None
        or candidate.validated_at is None
        or candidate.approved_at is None
        or candidate.terminal_at is None
        or not _at_or_before(candidate.validated_at, candidate.approved_at)
        or not _at_or_before(candidate.approved_at, candidate.terminal_at)
    ):
        raise RestoreControlError(
            "rollback-burned release candidate lacks valid validation/approval/burn chronology"
        )

    bundles = session.scalars(
        select(release_models.EvidenceBundle).where(
            release_models.EvidenceBundle.candidate_id == candidate.candidate_id,
            release_models.EvidenceBundle.bundle_kind == "release_candidate",
            release_models.EvidenceBundle.manifest_sha256
            == candidate.validation_bundle_sha256,
        )
    ).all()
    if len(bundles) != 1:
        raise RestoreControlError(
            "rollback-burned release candidate lacks one exact validation bundle"
        )
    bundle = bundles[0]
    if sha256_json(bundle.manifest) != bundle.manifest_sha256:
        raise RestoreControlError(
            "rollback-burned release candidate validation bundle is corrupt"
        )

    approval = session.scalar(
        select(release_models.CutoverApproval).where(
            release_models.CutoverApproval.candidate_id == candidate.candidate_id
        )
    )
    if (
        approval is None
        or approval.evidence_bundle_id != bundle.bundle_id
        or not _same_instant(approval.approved_at, candidate.approved_at)
    ):
        raise RestoreControlError(
            "rollback-burned release candidate approval evidence is inconsistent"
        )
    approval_at = _utc_instant(approval.approved_at)
    approval_body = {
        "candidate_id": str(candidate.candidate_id),
        "evidence_bundle_sha256": bundle.manifest_sha256,
        "approver": approval.approver,
        "statement": approval.approval_statement,
        "payload": dict(approval.approval_payload),
        "approved_at": approval_at.isoformat(),
    }
    if sha256_json(approval_body) != approval.approval_sha256:
        raise RestoreControlError(
            "rollback-burned release candidate approval digest is corrupt"
        )

    binding = session.scalar(
        select(manifest_models.CutoverApprovalManifestBinding).where(
            manifest_models.CutoverApprovalManifestBinding.approval_id
            == approval.approval_id
        )
    )
    manifest = (
        None
        if binding is None
        else session.get(manifest_models.ReleaseCandidateManifest, binding.manifest_id)
    )
    if (
        binding is None
        or binding.candidate_id != candidate.candidate_id
        or manifest is None
        or manifest.candidate_id != candidate.candidate_id
        or manifest.generation_id != candidate.generation_id
        or manifest.manifest_version != binding.manifest_version
        or manifest.canonical_fingerprint != binding.canonical_fingerprint
        or manifest.source_import_batch_id != candidate.source_import_batch_id
        or manifest.projection_epoch_id != candidate.projection_epoch_id
        or not _same_instant(binding.bound_at, approval.approved_at)
        or not _same_instant(manifest.built_at, approval.approved_at)
    ):
        raise RestoreControlError(
            "rollback-burned release candidate manifest binding is inconsistent"
        )

    activations = session.scalars(
        select(models.AuthorityActivation).where(
            models.AuthorityActivation.generation_id == active.generation_id,
            models.AuthorityActivation.outcome == "activated",
            models.AuthorityActivation.rehearsal_id.is_(None),
        )
    ).all()
    if len(activations) != 1:
        raise RestoreControlError(
            "rollback-burned release candidate lacks one exact activation evidence row"
        )
    activation = activations[0]
    if (
        activation.generation_id != candidate.generation_id
        or activation.cutover_approval_id != str(approval.approval_id)
        or activation.import_run_id != manifest.source_import_run_id
        or activation.projection_epoch != candidate.projection_epoch_id
        or activation.schema_head != candidate.schema_head
        or activation.dish_release != candidate.dish_release
        or activation.honest_release != candidate.honest_release
        or activation.protocol_release != candidate.protocol_release
        or activation.openapi_release != candidate.openapi_release
        or activation.routing_release != candidate.routing_release
        or not isinstance(activation.legacy_bundle_id, str)
        or not activation.legacy_bundle_id.strip()
        or activation.rollback_burned_at is None
        or not _same_instant(activation.rollback_burned_at, candidate.terminal_at)
        or not _same_instant(activation.recorded_at, candidate.terminal_at)
    ):
        raise RestoreControlError(
            "rollback-burned release candidate lacks exact activation evidence"
        )
    return candidate


def _assert_physical_recovery_binding(
    control: RestoreControl, recovered: RecoveredPhysicalState
) -> None:
    expected = {
        "database_name": control.expected_database_name,
        "system_identifier": control.expected_system_identifier,
        "schema_head": control.schema_head,
        "backup_manifest_sha256": control.backup_manifest_sha256,
        "backup_evidence_sha256": control.backup_evidence_sha256,
        "recovery_timeline_id": control.recovery_timeline_id,
        "recovery_target_type": control.recovery_target_type,
        "recovery_target_lsn": control.recovery_target_lsn,
        "recovery_completion_lsn": control.recovery_completion_lsn,
        "recovery_target_instance_sha256": (
            control.recovery_target_instance_sha256
        ),
    }
    observed = recovered.evidence_payload()
    mismatches = [key for key, value in expected.items() if observed.get(key) != value]
    if mismatches:
        raise RestoreControlError(
            "restore control physical recovery evidence mismatch: " + ", ".join(mismatches)
        )
    if recovered.evidence_sha256 != control.recovery_evidence_sha256:
        raise RestoreControlError("restore control recovery evidence digest mismatch")


def _current_recovery_generation(
    session: Session,
    *,
    control: RestoreControl,
    recovered_state: RecoveredPhysicalState,
) -> models.AuthorityGeneration:
    """Validate the recovered state that must be healthy for promotion now."""
    _assert_physical_recovery_binding(control, recovered_state)
    if recovered_state.schema_head != ALEMBIC_HEAD:
        raise RestoreControlError(
            "recovered schema head does not match the current migration head"
        )
    active = session.scalar(
        select(models.AuthorityGeneration)
        .where(models.AuthorityGeneration.status == "active")
        .with_for_update()
    )
    if active is None or active.generation_id != control.predecessor_generation_id:
        raise RestoreControlError(
            "restore control predecessor is not the recovered active generation"
        )
    if active.schema_head != control.schema_head or active.dish_release != control.dish_release:
        raise RestoreControlError(
            "recovered active generation release provenance does not match control"
        )
    return active



def _deterministic_rehydration_uuid(control: RestoreControl, label: str) -> uuid.UUID:
    namespace = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"dish:recovery-rehydration:{control.external_control_id}:{control.generation_id}",
    )
    return uuid.uuid5(namespace, label)


def _rehydration_repair_event(session: Session, generation_id: uuid.UUID) -> models.AppliedMigrationEvent | None:
    return session.scalar(
        select(models.AppliedMigrationEvent).where(
            models.AppliedMigrationEvent.generation_id == generation_id,
            models.AppliedMigrationEvent.revision == RECOVERY_REHYDRATION_REVISION,
            models.AppliedMigrationEvent.outcome == "repair",
        )
    )


def _validate_rehydration_lineage(
    session: Session,
    control: RestoreControl,
    recovered_state: RecoveredPhysicalState,
) -> tuple[models.AuthorityGeneration, models.AuthorityGeneration, models.ActiveSectionRegistry]:
    _assert_physical_recovery_binding(control, recovered_state)
    successor = session.scalar(
        select(models.AuthorityGeneration)
        .where(models.AuthorityGeneration.generation_id == control.generation_id)
        .with_for_update()
    )
    predecessor = session.get(models.AuthorityGeneration, control.predecessor_generation_id)
    if successor is None or predecessor is None:
        raise RestoreControlError("rehydration requires the exact promoted predecessor/successor lineage")
    if (
        successor.status != "active"
        or successor.creation_reason != "destructive_restore"
        or successor.predecessor_generation_id != predecessor.generation_id
        or successor.external_restore_control_id != control.external_control_id
        or successor.schema_head != control.schema_head
        or successor.dish_release != control.dish_release
        or predecessor.status != "retired"
    ):
        raise RestoreControlError("rehydration predecessor/successor lineage mismatch")
    bootstrap = session.get(models.GenerationBootstrapAuthority, control.bootstrap_id)
    if (
        bootstrap is None
        or bootstrap.generation_id != successor.generation_id
        or bootstrap.external_control_id != control.external_control_id
        or bootstrap.capability_digest != control.bootstrap_capability_digest
    ):
        raise RestoreControlError("rehydration recovery authority mismatch")
    activation = session.scalar(
        select(models.AuthorityActivation).where(
            models.AuthorityActivation.generation_id == successor.generation_id,
            models.AuthorityActivation.outcome == "activated",
        )
    )
    if (
        activation is None
        or activation.cutover_approval_id != control.external_control_id
        or activation.schema_head != control.schema_head
        or activation.dish_release != control.dish_release
        or activation.honest_release != control.honest_release
        or activation.protocol_release != control.protocol_release
        or activation.openapi_release != control.openapi_release
        or activation.routing_release != control.routing_release
    ):
        raise RestoreControlError("rehydration recovery release authority mismatch")
    stamp = session.scalar(
        select(models.AppliedMigrationEvent).where(
            models.AppliedMigrationEvent.generation_id == successor.generation_id,
            models.AppliedMigrationEvent.revision == control.schema_head,
            models.AppliedMigrationEvent.outcome == "stamp",
        )
    )
    if (
        stamp is None
        or stamp.details.get("external_restore_control_id") != control.external_control_id
        or stamp.details.get("recovery_evidence_sha256") != control.recovery_evidence_sha256
    ):
        raise RestoreControlError("rehydration requires exact restore-promotion evidence")
    registry = session.get(models.ActiveSectionRegistry, successor.generation_id)
    if registry is None:
        raise RestoreControlError("rehydration successor lacks active section registry")
    epoch = session.scalar(
        select(projection_models.ProjectionEpoch).where(
            projection_models.ProjectionEpoch.generation_id == successor.generation_id,
            projection_models.ProjectionEpoch.status == "active",
        )
    )
    if epoch is None or epoch.external_effects_enabled:
        raise RestoreControlError(
            "rehydration requires the successor projection epoch to remain external-effects-disabled"
        )
    return predecessor, successor, registry


def _predecessor_task_snapshot(session: Session, generation_id: uuid.UUID) -> list[dict[str, Any]]:
    heads = session.scalars(
        select(models.TaskAuthorityHead)
        .where(models.TaskAuthorityHead.generation_id == generation_id)
        .order_by(models.TaskAuthorityHead.task_id)
    ).all()
    snapshots: list[dict[str, Any]] = []
    for head in heads:
        activation = session.get(models.ContentActivation, head.current_content_activation_id)
        placement = session.get(models.CurrentTaskSectionPlacement, (generation_id, head.task_id))
        completion = session.get(models.CurrentTaskCompletion, (generation_id, head.task_id))
        if (
            activation is None
            or activation.generation_id != generation_id
            or activation.task_id != head.task_id
            or activation.task_revision != head.task_revision
            or placement is None
            or placement.placement_revision != head.placement_revision
            or completion is None
            or completion.completion_revision != head.completion_revision
        ):
            raise RestoreControlError(
                f"predecessor current task authority is incomplete for task {head.task_id}"
            )
        version = session.get(models.ContentVersion, activation.content_version_id)
        if version is None or version.generation_id != generation_id or version.task_id != head.task_id:
            raise RestoreControlError(
                f"predecessor current content is incomplete for task {head.task_id}"
            )
        memberships = session.scalars(
            select(models.CurrentTaskProjectMembership)
            .where(
                models.CurrentTaskProjectMembership.generation_id == generation_id,
                models.CurrentTaskProjectMembership.task_id == head.task_id,
            )
            .order_by(models.CurrentTaskProjectMembership.project_id)
        ).all()
        if any(row.membership_revision > head.membership_revision for row in memberships):
            raise RestoreControlError(
                f"predecessor membership revision exceeds task head for task {head.task_id}"
            )
        snapshots.append(
            {
                "task_id": str(head.task_id),
                "source_content_version_id": str(version.content_version_id),
                "source_content_activation_id": str(activation.content_activation_id),
                "representation_kind": version.representation_kind,
                "title": version.title,
                "body": version.body,
                "identity_scheme": version.identity_scheme,
                "content_identity": version.content_identity,
                "contract_binding_id": str(version.contract_binding_id),
                "task_revision": head.task_revision,
                "membership_revision": head.membership_revision,
                "placement_revision": head.placement_revision,
                "completion_revision": head.completion_revision,
                "memberships": [
                    {
                        "project_id": str(row.project_id),
                        "is_member": bool(row.is_member),
                        "membership_revision": row.membership_revision,
                        "source_event_id": str(row.latest_event_id),
                    }
                    for row in memberships
                ],
                "placement": {
                    "section_id": None if placement.section_id is None else str(placement.section_id),
                    "source_registry_version_id": str(placement.registry_version_id),
                    "source_event_id": str(placement.latest_event_id),
                },
                "completion": {
                    "completed": bool(completion.completed),
                    "source_event_id": str(completion.latest_event_id),
                },
            }
        )
    return snapshots


def _predecessor_transient_state(session: Session, generation_id: uuid.UUID) -> dict[str, Any]:
    operations = session.scalars(
        select(wf.WorkflowOperation)
        .where(wf.WorkflowOperation.generation_id == generation_id, wf.WorkflowOperation.lifecycle == "open")
        .order_by(wf.WorkflowOperation.operation_id)
    ).all()
    leases = session.scalars(
        select(wf.ServiceLease)
        .where(wf.ServiceLease.generation_id == generation_id, wf.ServiceLease.state == "active")
        .order_by(wf.ServiceLease.lease_id)
    ).all()
    requests = session.scalars(
        select(wf.ServiceRequest)
        .where(wf.ServiceRequest.generation_id == generation_id)
        .order_by(wf.ServiceRequest.request_id)
    ).all()
    unresolved_requests: list[dict[str, Any]] = []
    for request in requests:
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(wf.ServiceRequestOutcome.request_id == request.request_id)
        )
        resolution = session.scalar(
            select(wf.RequestUncertaintyResolution).where(
                wf.RequestUncertaintyResolution.request_id == request.request_id
            )
        )
        if outcome is None or (outcome.outcome_class == "uncertain" and resolution is None):
            unresolved_requests.append(
                {
                    "request_id": str(request.request_id),
                    "command_name": request.command_name,
                    "outcome_class": None if outcome is None else outcome.outcome_class,
                    "classification": "forensic_only_no_reissue",
                }
            )
    executions = session.scalars(
        select(wf.CommandExecution)
        .where(
            wf.CommandExecution.generation_id == generation_id,
            wf.CommandExecution.status.in_(("pending", "claimed", "uncertain")),
        )
        .order_by(wf.CommandExecution.execution_id)
    ).all()
    projection_events = session.scalars(
        select(projection_models.ProjectionOutboxEvent)
        .where(projection_models.ProjectionOutboxEvent.generation_id == generation_id)
        .order_by(projection_models.ProjectionOutboxEvent.projection_event_id)
    ).all()
    unresolved_effects: list[dict[str, Any]] = []
    for event in projection_events:
        attempts = session.scalars(
            select(projection_models.ProjectionAttempt)
            .where(projection_models.ProjectionAttempt.projection_event_id == event.projection_event_id)
            .order_by(projection_models.ProjectionAttempt.attempt_number)
        ).all()
        attempt_states = [attempt.state for attempt in attempts]
        if event.state in {"pending", "claimed", "uncertain", "blocked"} or any(
            state in {"dispatched", "uncertain", "blocked"} for state in attempt_states
        ):
            unresolved_effects.append(
                {
                    "projection_event_id": str(event.projection_event_id),
                    "task_id": str(event.task_id),
                    "event_type": event.event_type,
                    "event_state": event.state,
                    "attempts": [
                        {"attempt_id": str(a.attempt_id), "state": a.state, "attempt_number": a.attempt_number}
                        for a in attempts
                    ],
                    "classification": "predecessor_only_reconcile_never_redispatch",
                }
            )
    return {
        "open_operations": [
            {
                "operation_id": str(row.operation_id),
                "task_id": str(row.task_id),
                "kind": row.kind,
                "phase": row.phase,
                "classification": "fenced_predecessor_only",
            }
            for row in operations
        ],
        "active_leases": [
            {
                "lease_id": str(row.lease_id),
                "task_id": str(row.task_id),
                "operation_id": None if row.operation_id is None else str(row.operation_id),
                "run_id": None if row.run_id is None else str(row.run_id),
                "classification": "fenced_predecessor_only",
            }
            for row in leases
        ],
        "unresolved_requests": unresolved_requests,
        "in_flight_executions": [
            {
                "execution_id": str(row.execution_id),
                "request_id": str(row.request_id),
                "task_id": None if row.task_id is None else str(row.task_id),
                "status": row.status,
                "classification": "forensic_only_no_reissue",
            }
            for row in executions
        ],
        "unresolved_external_effects": unresolved_effects,
        "carry_forward_policy": {
            "workflow_operations": "none",
            "leases": "none",
            "service_requests": "none",
            "command_executions": "none",
            "projection_outbox_or_attempts": "none",
        },
    }


def _clone_rehydrated_task_authority(
    session: Session,
    *,
    control: RestoreControl,
    import_run_id: uuid.UUID,
    registry_version_id: uuid.UUID,
    snapshots: list[dict[str, Any]],
    at: datetime,
) -> None:
    for snapshot in snapshots:
        task_id = uuid.UUID(snapshot["task_id"])
        version_id = _deterministic_rehydration_uuid(control, f"task:{task_id}:content-version")
        activation_id = _deterministic_rehydration_uuid(control, f"task:{task_id}:content-activation")
        session.add(
            models.ContentVersion(
                content_version_id=version_id,
                generation_id=control.generation_id,
                task_id=task_id,
                representation_kind=snapshot["representation_kind"],
                title=snapshot["title"],
                body=snapshot["body"],
                identity_scheme=snapshot["identity_scheme"],
                content_identity=snapshot["content_identity"],
                creator_route="import",
                import_run_id=import_run_id,
                command_execution_id=None,
                predecessor_content_version_id=None,
                contract_binding_id=uuid.UUID(snapshot["contract_binding_id"]),
                created_at=at,
            )
        )
        session.flush()
        session.add(
            models.ContentActivation(
                content_activation_id=activation_id,
                generation_id=control.generation_id,
                task_id=task_id,
                content_version_id=version_id,
                activation_route="import",
                import_run_id=import_run_id,
                command_execution_id=None,
                task_revision=snapshot["task_revision"],
                activated_at=at,
            )
        )
        session.flush()
        session.add(
            models.TaskAuthorityHead(
                generation_id=control.generation_id,
                task_id=task_id,
                current_content_activation_id=activation_id,
                task_revision=snapshot["task_revision"],
                membership_revision=snapshot["membership_revision"],
                placement_revision=snapshot["placement_revision"],
                completion_revision=snapshot["completion_revision"],
                updated_at=at,
            )
        )
        session.flush()
        for membership in snapshot["memberships"]:
            project_id = uuid.UUID(membership["project_id"])
            event_id = _deterministic_rehydration_uuid(control, f"task:{task_id}:membership:{project_id}")
            session.add(
                models.TaskProjectMembershipEvent(
                    membership_event_id=event_id,
                    generation_id=control.generation_id,
                    task_id=task_id,
                    project_id=project_id,
                    event_kind="joined" if membership["is_member"] else "left",
                    membership_revision=membership["membership_revision"],
                    provenance_route="import",
                    import_run_id=import_run_id,
                    command_execution_id=None,
                    occurred_at=at,
                )
            )
            session.flush()
            session.add(
                models.CurrentTaskProjectMembership(
                    generation_id=control.generation_id,
                    task_id=task_id,
                    project_id=project_id,
                    latest_event_id=event_id,
                    is_member=membership["is_member"],
                    membership_revision=membership["membership_revision"],
                    updated_at=at,
                )
            )
        placement_id = _deterministic_rehydration_uuid(control, f"task:{task_id}:placement")
        section_id = None if snapshot["placement"]["section_id"] is None else uuid.UUID(snapshot["placement"]["section_id"])
        session.add(
            models.TaskSectionPlacementEvent(
                placement_event_id=placement_id,
                generation_id=control.generation_id,
                task_id=task_id,
                section_id=section_id,
                registry_version_id=registry_version_id,
                event_kind="placed" if section_id is not None else "cleared",
                placement_revision=snapshot["placement_revision"],
                provenance_route="import",
                import_run_id=import_run_id,
                command_execution_id=None,
                occurred_at=at,
            )
        )
        session.flush()
        session.add(
            models.CurrentTaskSectionPlacement(
                generation_id=control.generation_id,
                task_id=task_id,
                section_id=section_id,
                registry_version_id=registry_version_id,
                latest_event_id=placement_id,
                placement_revision=snapshot["placement_revision"],
                updated_at=at,
            )
        )
        completion_id = _deterministic_rehydration_uuid(control, f"task:{task_id}:completion")
        session.add(
            models.TaskCompletionEvent(
                completion_event_id=completion_id,
                generation_id=control.generation_id,
                task_id=task_id,
                completed=snapshot["completion"]["completed"],
                reason="imported",
                completion_revision=snapshot["completion_revision"],
                provenance_route="import",
                import_run_id=import_run_id,
                command_execution_id=None,
                occurred_at=at,
            )
        )
        session.flush()
        session.add(
            models.CurrentTaskCompletion(
                generation_id=control.generation_id,
                task_id=task_id,
                completed=snapshot["completion"]["completed"],
                latest_event_id=completion_id,
                completion_revision=snapshot["completion_revision"],
                updated_at=at,
            )
        )
    session.flush()


def rehydrate_restored_generation(
    session: Session,
    control: RestoreControl,
    *,
    recovered_state: RecoveredPhysicalState,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> RecoveryRehydrationResult:
    """Reissue only current task authority into an exact promoted restore successor.

    Historical workflow ownership and projection/external-effect state remain exclusively in
    the retired predecessor.  The immutable repair event is the admission gate proving that
    this exact successor was rehydrated under the exact restore receipt.
    """
    predecessor, successor, registry = _validate_rehydration_lineage(
        session, control, recovered_state
    )
    snapshots = _predecessor_task_snapshot(session, predecessor.generation_id)
    snapshot_sha = sha256_json(
        {
            "format": "dish-recovery-current-task-snapshot-v1",
            "predecessor_generation_id": str(predecessor.generation_id),
            "successor_generation_id": str(successor.generation_id),
            "tasks": snapshots,
        }
    )
    transient_state = _predecessor_transient_state(session, predecessor.generation_id)
    transient_sha = sha256_json(
        {
            "format": "dish-recovery-transient-classification-v1",
            "predecessor_generation_id": str(predecessor.generation_id),
            "state": transient_state,
        }
    )
    existing = _rehydration_repair_event(session, successor.generation_id)
    if existing is not None:
        details = existing.details
        if (
            details.get("route") != RECOVERY_REHYDRATION_REVISION
            or details.get("external_restore_control_id") != control.external_control_id
            or details.get("predecessor_generation_id") != str(predecessor.generation_id)
            or details.get("successor_generation_id") != str(successor.generation_id)
            or details.get("predecessor_snapshot_sha256") != snapshot_sha
            or details.get("recovery_evidence_sha256") != control.recovery_evidence_sha256
            or details.get("bootstrap_id") != str(control.bootstrap_id)
            or details.get("bootstrap_capability_sha256")
                != control.bootstrap_capability_digest.hex()
        ):
            raise RestoreControlError("existing recovery rehydration evidence conflicts with exact lineage")
        return RecoveryRehydrationResult(
            predecessor_generation_id=predecessor.generation_id,
            generation_id=successor.generation_id,
            import_run_id=uuid.UUID(details["import_run_id"]),
            repair_event_id=existing.migration_event_id,
            predecessor_snapshot_sha256=snapshot_sha,
            transient_state_sha256=str(details["transient_state_sha256"]),
            task_count=int(details["task_count"]),
            rehydrated_at=existing.terminal_at,
            replayed=True,
        )
    if session.scalar(
        select(models.TaskAuthorityHead.generation_id)
        .where(models.TaskAuthorityHead.generation_id == successor.generation_id)
        .limit(1)
    ) is not None:
        raise RestoreControlError("successor already contains task authority without authorized rehydration")
    at = clock()
    if at.tzinfo is None or at.utcoffset() is None:
        raise RestoreControlError("rehydration clock must be timezone-aware")
    import_run_id = _deterministic_rehydration_uuid(control, "authority-import-run")
    repair_event_id = _deterministic_rehydration_uuid(control, "repair-event")
    session.add(
        models.ImportRun(
            import_run_id=import_run_id,
            source_commit=control.recovery_evidence_sha256,
            source_release=predecessor.dish_release,
            legacy_generation_id=f"recovery:{predecessor.generation_id}",
            baseline_high_water_mark=f"{successor.generation_id}:{snapshot_sha}",
            source_bundle_sha256=snapshot_sha,
            status="complete",
            started_at=at,
            completed_at=at,
            provenance={
                "route": RECOVERY_REHYDRATION_REVISION,
                "external_restore_control_id": control.external_control_id,
                "predecessor_generation_id": str(predecessor.generation_id),
                "successor_generation_id": str(successor.generation_id),
                "recovery_evidence_sha256": control.recovery_evidence_sha256,
                "predecessor_snapshot_sha256": snapshot_sha,
                "bootstrap_id": str(control.bootstrap_id),
                "bootstrap_capability_sha256": control.bootstrap_capability_digest.hex(),
            },
        )
    )
    session.flush()
    _clone_rehydrated_task_authority(
        session,
        control=control,
        import_run_id=import_run_id,
        registry_version_id=registry.registry_version_id,
        snapshots=snapshots,
        at=at,
    )
    repair_code_sha = hashlib.sha256(RECOVERY_REHYDRATION_REVISION.encode("utf-8")).hexdigest()
    AuthorityRepository(session).add_migration_event(
        models.AppliedMigrationEvent(
            migration_event_id=repair_event_id,
            generation_id=successor.generation_id,
            revision=RECOVERY_REHYDRATION_REVISION,
            predecessor_revision=control.schema_head,
            migration_code_sha256=repair_code_sha,
            dish_release=control.dish_release,
            initiator="dish-pg-recovery-rehydration",
            outcome="repair",
            started_at=at,
            terminal_at=at,
            details={
                "route": RECOVERY_REHYDRATION_REVISION,
                "external_restore_control_id": control.external_control_id,
                "predecessor_generation_id": str(predecessor.generation_id),
                "successor_generation_id": str(successor.generation_id),
                "recovery_evidence_sha256": control.recovery_evidence_sha256,
                "bootstrap_id": str(control.bootstrap_id),
                "bootstrap_capability_sha256": control.bootstrap_capability_digest.hex(),
                "predecessor_snapshot_sha256": snapshot_sha,
                "transient_state_sha256": transient_sha,
                "transient_state": transient_state,
                "import_run_id": str(import_run_id),
                "task_count": len(snapshots),
                "carried_forward": [
                    "current_task_authority",
                    "current_content_binding",
                    "current_membership",
                    "current_placement",
                    "current_completion",
                ],
                "fenced_not_carried": [
                    "workflow_operations",
                    "leases",
                    "service_requests",
                    "command_executions",
                    "projection_outbox_events",
                    "projection_attempts",
                ],
                "external_effects_enabled": False,
            },
        )
    )
    return RecoveryRehydrationResult(
        predecessor_generation_id=predecessor.generation_id,
        generation_id=successor.generation_id,
        import_run_id=import_run_id,
        repair_event_id=repair_event_id,
        predecessor_snapshot_sha256=snapshot_sha,
        transient_state_sha256=transient_sha,
        task_count=len(snapshots),
        rehydrated_at=at,
        replayed=False,
    )

def promote_restored_generation(
    session: Session,
    control: RestoreControl,
    *,
    recovered_state: RecoveredPhysicalState,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> RestorePromotionResult:
    """Promote a recovered database only under a matching external receipt."""
    at = clock()
    if at.tzinfo is None or at.utcoffset() is None:
        raise RestoreControlError("restore promotion clock must be timezone-aware")
    if control.issued_at > at:
        raise RestoreControlError("restore control was issued in the future")

    authority = AuthorityRepository(session)
    active = _current_recovery_generation(
        session,
        control=control,
        recovered_state=recovered_state,
    )
    _authorized_release_candidate(session, active=active, control=control)
    if session.get(models.AuthorityGeneration, control.generation_id) is not None:
        raise RestoreControlError("restore generation already exists")

    projection_epoch_id = uuid_factory()
    authority.add_generation(
        models.AuthorityGeneration(
            generation_id=control.generation_id,
            predecessor_generation_id=active.generation_id,
            creation_reason="destructive_restore",
            external_restore_control_id=control.external_control_id,
            schema_head=control.schema_head,
            dish_release=control.dish_release,
            status="pending",
            created_at=at,
            retired_at=None,
        )
    )
    authority.add_bootstrap_authority(
        models.GenerationBootstrapAuthority(
            bootstrap_id=control.bootstrap_id,
            generation_id=control.generation_id,
            external_control_id=control.external_control_id,
            capability_digest=control.bootstrap_capability_digest,
            issued_at=control.issued_at,
            consumed_at=None,
            retired_at=None,
        )
    )
    prior_registry = session.get(models.ActiveSectionRegistry, active.generation_id)
    if prior_registry is None:
        raise RestoreControlError("recovered active generation lacks registry authority")
    prior_version = session.get(models.SectionRegistryVersion, prior_registry.registry_version_id)
    if prior_version is None:
        raise RestoreControlError("recovered registry version is missing")
    activation = models.AuthorityActivation(
        activation_id=uuid_factory(),
        generation_id=control.generation_id,
        import_run_id=prior_version.import_run_id,
        cutover_approval_id=control.external_control_id,
        legacy_bundle_id=f"postgresql-restore:{control.external_control_id}",
        schema_head=control.schema_head,
        dish_release=control.dish_release,
        honest_release=control.honest_release,
        protocol_release=control.protocol_release,
        openapi_release=control.openapi_release,
        routing_release=control.routing_release,
        projection_epoch=projection_epoch_id,
        outcome="activated",
        rollback_burned_at=at,
        recorded_at=at,
    )
    authority.activate_generation(generation_id=control.generation_id, activation=activation, at=at)

    old_epochs = session.scalars(
        select(projection_models.ProjectionEpoch).where(
            projection_models.ProjectionEpoch.generation_id == active.generation_id,
            projection_models.ProjectionEpoch.status == "active",
        )
    ).all()
    for epoch in old_epochs:
        epoch.status = "retired"
        epoch.external_effects_enabled = False
        epoch.retired_at = at
    session.flush()

    registry_version_id, registry_activation_id = _clone_registry(
        session,
        predecessor_generation_id=active.generation_id,
        generation_id=control.generation_id,
        at=at,
        uuid_factory=uuid_factory,
    )
    ProjectionService(session, uuid_factory=lambda: projection_epoch_id).activate_epoch(
        generation_id=control.generation_id,
        activation_reason=f"destructive restore under {control.external_control_id}",
        created_at=at,
        external_effects_enabled=False,
    )
    migration_event_id = uuid_factory()
    authority.add_migration_event(
        models.AppliedMigrationEvent(
            migration_event_id=migration_event_id,
            generation_id=control.generation_id,
            revision=control.schema_head,
            predecessor_revision=control.schema_head,
            migration_code_sha256=migration_revision_sha256(control.schema_head),
            dish_release=control.dish_release,
            initiator="dish-pg-recovery-rehearsal",
            outcome="stamp",
            started_at=at,
            terminal_at=at,
            details={
                "external_restore_control_id": control.external_control_id,
                "database_name": recovered_state.database_name,
                "system_identifier": recovered_state.system_identifier,
                "backup_manifest_sha256": recovered_state.backup_manifest_sha256,
                "backup_evidence_sha256": recovered_state.backup_evidence_sha256,
                "recovery_timeline_id": recovered_state.recovery_timeline_id,
                "recovery_target_type": recovered_state.recovery_target_type,
                "recovery_target_lsn": recovered_state.recovery_target_lsn,
                "recovery_completion_lsn": recovered_state.recovery_completion_lsn,
                "recovery_target_instance_sha256": (
                    recovered_state.recovery_target_instance_sha256
                ),
                "recovery_evidence_sha256": recovered_state.evidence_sha256,
                "route": "destructive_restore",
            },
        )
    )
    return RestorePromotionResult(
        predecessor_generation_id=active.generation_id,
        generation_id=control.generation_id,
        bootstrap_id=control.bootstrap_id,
        registry_version_id=registry_version_id,
        registry_activation_id=registry_activation_id,
        projection_epoch_id=projection_epoch_id,
        migration_event_id=migration_event_id,
        external_control_id=control.external_control_id,
        promoted_at=at,
    )
