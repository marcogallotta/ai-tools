"""Externally controlled authority promotion after PostgreSQL restore or PITR.

The database may contain a complete historical authority timeline after recovery,
but restored rows do not make restored actors current.  This module consumes one
operator-issued control receipt, creates a new destructive-restore generation,
clones only the governed registry needed for admission, and starts a disabled
projection epoch.  Normal workflow admission then requires the one-time bootstrap
capability attached to that receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import candidate_manifest_models as manifest_models
from .candidate_manifest import revalidate_candidate_manifest
from . import models
from . import stage5_models as projection_models
from . import stage6_models as release_models
from .release import ALEMBIC_HEAD
from .release_evidence import ReleaseAuthorityError, sha256_json
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


def _same_instant(left: datetime, right: datetime) -> bool:
    def normalized(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return normalized(left) == normalized(right)


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
    revalidated_at: datetime,
) -> release_models.ReleaseCandidate:
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
    if candidate.status not in {"approved", "activated"}:
        raise RestoreControlError(
            f"recovered release candidate state is not authorized: {candidate.status}"
        )
    if (
        candidate.validation_bundle_sha256 is None
        or candidate.validated_at is None
        or candidate.approved_at is None
    ):
        raise RestoreControlError("authorized release candidate lacks validation chronology")
    bundles = session.scalars(
        select(release_models.EvidenceBundle).where(
            release_models.EvidenceBundle.candidate_id == candidate.candidate_id,
            release_models.EvidenceBundle.bundle_kind == "release_candidate",
            release_models.EvidenceBundle.manifest_sha256
            == candidate.validation_bundle_sha256,
        )
    ).all()
    if len(bundles) != 1:
        raise RestoreControlError("authorized release candidate lacks one exact validation bundle")
    bundle = bundles[0]
    if sha256_json(bundle.manifest) != bundle.manifest_sha256:
        raise RestoreControlError("authorized release candidate validation bundle is corrupt")
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
        raise RestoreControlError("authorized release candidate approval evidence is inconsistent")
    approval_at = approval.approved_at
    if approval_at.tzinfo is None or approval_at.utcoffset() is None:
        approval_at = approval_at.replace(tzinfo=timezone.utc)
    else:
        approval_at = approval_at.astimezone(timezone.utc)
    approval_body = {
        "candidate_id": str(candidate.candidate_id),
        "evidence_bundle_sha256": bundle.manifest_sha256,
        "approver": approval.approver,
        "statement": approval.approval_statement,
        "payload": dict(approval.approval_payload),
        "approved_at": approval_at.isoformat(),
    }
    if sha256_json(approval_body) != approval.approval_sha256:
        raise RestoreControlError("authorized release candidate approval digest is corrupt")
    binding = session.scalar(
        select(manifest_models.CutoverApprovalManifestBinding).where(
            manifest_models.CutoverApprovalManifestBinding.approval_id == approval.approval_id
        )
    )
    manifest = None if binding is None else session.get(
        manifest_models.ReleaseCandidateManifest, binding.manifest_id
    )
    if (
        binding is None
        or binding.candidate_id != candidate.candidate_id
        or manifest is None
        or manifest.candidate_id != candidate.candidate_id
        or manifest.generation_id != active.generation_id
        or manifest.canonical_fingerprint != binding.canonical_fingerprint
    ):
        raise RestoreControlError("authorized release candidate manifest binding is inconsistent")
    try:
        revalidation = revalidate_candidate_manifest(
            session,
            uuid_factory=uuid.uuid4,
            candidate=candidate,
            revalidated_at=revalidated_at,
        )
    except ReleaseAuthorityError as exc:
        raise RestoreControlError(
            "authorized release candidate manifest could not be revalidated"
        ) from exc
    if revalidation.result != "matched":
        raise RestoreControlError("authorized release candidate manifest is stale")
    if candidate.status == "activated":
        activations = session.scalars(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == active.generation_id,
                models.AuthorityActivation.outcome == "activated",
                models.AuthorityActivation.cutover_approval_id == str(approval.approval_id),
            )
        ).all()
        if len(activations) != 1:
            raise RestoreControlError(
                "activated release candidate lacks one exact activation evidence row"
            )
        activation = activations[0]
        batch = session.get(
            projection_models.SourceImportBatch, candidate.source_import_batch_id
        )
        if (
            batch is None
            or candidate.terminal_at is None
            or activation.import_run_id != batch.import_run_id
            or activation.projection_epoch != candidate.projection_epoch_id
            or activation.schema_head != candidate.schema_head
            or activation.dish_release != candidate.dish_release
            or activation.honest_release != candidate.honest_release
            or activation.protocol_release != candidate.protocol_release
            or activation.openapi_release != candidate.openapi_release
            or activation.routing_release != candidate.routing_release
            or activation.rollback_burned_at is None
            or not _same_instant(activation.rollback_burned_at, candidate.terminal_at)
            or not _same_instant(activation.recorded_at, candidate.terminal_at)
        ):
            raise RestoreControlError("activated release candidate lacks exact activation evidence")
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
    _assert_physical_recovery_binding(control, recovered_state)
    if recovered_state.schema_head != ALEMBIC_HEAD:
        raise RestoreControlError(
            "recovered schema head does not match the current migration head"
        )

    authority = AuthorityRepository(session)
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
    _authorized_release_candidate(
        session, active=active, control=control, revalidated_at=at
    )
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
