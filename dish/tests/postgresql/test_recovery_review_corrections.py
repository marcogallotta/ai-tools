from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.candidate_manifest import revalidate_candidate_manifest
from dish_pg.database import session_scope
from dish_pg.recovery_control import (
    RestoreControlError,
    promote_restored_generation,
)
from dish_pg import recovery_rehearsal
from dish_pg.recovery_rehearsal import _cleanup_rehearsal, _source_manifest
from tests.support.postgresql.core import NOW, _activate_cloned_registry_revision
from tests.support.postgresql.workflow import NOW as WORKFLOW_NOW
from tests.support.postgresql.recovery_control import _control, _physical_state, recovery_db
from tests.support.postgresql.stage8_cutover_evidence_gates import _burn_rollback


def _control_for_candidate(context, ids, state, candidate):
    return replace(
        _control(context, ids, state),
        schema_head=candidate.schema_head,
        dish_release=candidate.dish_release,
        honest_release=candidate.honest_release,
        protocol_release=candidate.protocol_release,
        openapi_release=candidate.openapi_release,
        routing_release=candidate.routing_release,
    )



def _burn_recovery_predecessor(session, ids, context, task_id):
    generation = session.get(models.AuthorityGeneration, context["generation_id"])
    assert generation is not None
    service, candidate_id, _cutover_run_id = _burn_rollback(
        session, ids, context, task_id, dish_release=generation.dish_release
    )
    assert service._candidate(candidate_id).status == "activated"
    return candidate_id


def test_synthetic_activation_corruption_requires_exact_activation_lineage(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"]
            )
        )
        assert candidate is not None and activation is not None
        activation.projection_epoch = uuid.uuid4()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="exact activation evidence"):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )


def test_legitimate_activated_candidate_can_promote(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        state = _physical_state()
        result = promote_restored_generation(
            session,
            _control_for_candidate(context, ids, state, candidate),
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
        )
        assert result.predecessor_generation_id == context["generation_id"]


def test_legitimate_burned_recovery_authority_survives_runtime_mapping_evolution(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        mapping = session.scalar(
            select(projection_models.ProjectProjectionMapping).where(
                projection_models.ProjectProjectionMapping.generation_id
                == candidate.generation_id,
                projection_models.ProjectProjectionMapping.projection_epoch_id
                == candidate.projection_epoch_id,
                projection_models.ProjectProjectionMapping.state == "active",
            )
        )
        assert mapping is not None
        mapping.mapping_revision += 1
        mapping.bound_at = WORKFLOW_NOW + timedelta(minutes=7)
        session.flush()
        revalidation = revalidate_candidate_manifest(
            session,
            uuid_factory=lambda: next(ids),
            candidate=candidate,
            revalidated_at=WORKFLOW_NOW + timedelta(minutes=8),
        )
        assert revalidation.result == "stale"

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        state = _physical_state()
        result = promote_restored_generation(
            session,
            _control_for_candidate(context, ids, state, candidate),
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=10),
        )
        assert result.predecessor_generation_id == context["generation_id"]


def test_recovery_accepts_current_coherent_registry_evolution_and_clones_it(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        _activate_cloned_registry_revision(
            session,
            ids,
            generation_id=candidate.generation_id,
            registry_sha256="d" * 64,
            activated_at=WORKFLOW_NOW + timedelta(minutes=7),
        )

    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        source = session.scalar(
            select(models.SectionRegistryVersion).where(
                models.SectionRegistryVersion.registry_sha256 == "d" * 64
            )
        )
        assert source is not None
        state = _physical_state()
        result = promote_restored_generation(
            session,
            _control_for_candidate(context, ids, state, candidate),
            recovered_state=state,
            clock=lambda: WORKFLOW_NOW + timedelta(minutes=10),
        )
        cloned = session.get(models.SectionRegistryVersion, result.registry_version_id)
        assert cloned is not None
        assert cloned.registry_sha256 == "d" * 64
        assert cloned.import_run_id == source.import_run_id
        assert cloned.contract_binding_id == source.contract_binding_id



@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda activation: setattr(activation, "cutover_approval_id", "wrong-approval"), "exact activation evidence"),
        (lambda activation: setattr(activation, "import_run_id", uuid.uuid4()), "exact activation evidence"),
        (lambda activation: setattr(activation, "projection_epoch", uuid.uuid4()), "exact activation evidence"),
        (lambda activation: setattr(activation, "protocol_release", "wrong-protocol"), "exact activation evidence"),
        (lambda activation: setattr(activation, "generation_id", uuid.uuid4()), "exact activation evidence"),
        (lambda activation: setattr(activation, "legacy_bundle_id", "   "), "exact activation evidence"),
        (
            lambda activation: setattr(
                activation, "rollback_burned_at", NOW + timedelta(seconds=1)
            ),
            "exact activation evidence",
        ),
        (
            lambda activation: setattr(
                activation, "recorded_at", NOW + timedelta(seconds=1)
            ),
            "exact activation evidence",
        ),
    ],
    ids=[
        "approval",
        "import-run",
        "projection",
        "release",
        "generation",
        "legacy-bundle",
        "rollback-burn-time",
        "recorded-time",
    ],
)
def test_synthetic_burned_candidate_corruption_requires_exact_activation_authority(
    recovery_db, mutation, message
):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        activation = session.scalar(
            select(models.AuthorityActivation).where(
                models.AuthorityActivation.generation_id == context["generation_id"]
            )
        )
        assert candidate is not None and activation is not None
        mutation(activation)
        state = _physical_state()
        with pytest.raises(RestoreControlError, match=message):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )


def test_synthetic_corrupt_release_candidate_evidence_bundle_cannot_promote(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        bundle = session.scalar(
            select(release_models.EvidenceBundle).where(
                release_models.EvidenceBundle.candidate_id == candidate_id,
                release_models.EvidenceBundle.bundle_kind == "release_candidate",
            )
        )
        assert bundle is not None
        bundle.manifest = {"candidate_id": str(candidate_id), "authorized": False}
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="validation bundle is corrupt"):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )


def test_synthetic_broken_approval_manifest_identity_bridge_cannot_promote(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        binding = session.scalar(
            select(manifest_models.CutoverApprovalManifestBinding).where(
                manifest_models.CutoverApprovalManifestBinding.candidate_id == candidate_id
            )
        )
        assert candidate is not None and binding is not None
        binding.candidate_id = uuid.uuid4()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="manifest binding is inconsistent"):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )


def test_synthetic_broken_manifest_source_import_identity_bridge_cannot_promote(recovery_db):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        manifest = session.scalar(
            select(manifest_models.ReleaseCandidateManifest).where(
                manifest_models.ReleaseCandidateManifest.candidate_id == candidate_id
            )
        )
        assert candidate is not None and manifest is not None
        manifest.source_import_batch_id = uuid.uuid4()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="manifest binding is inconsistent"):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )



@pytest.mark.parametrize(
    "field,value",
    [
        ("validated_at", WORKFLOW_NOW + timedelta(minutes=5, seconds=1)),
        ("approved_at", WORKFLOW_NOW + timedelta(minutes=6, seconds=1)),
    ],
    ids=["validation-after-approval", "approval-after-burn"],
)
def test_synthetic_invalid_candidate_authority_chronology_cannot_promote(
    recovery_db, field, value
):
    factory, ids, context, task_id = recovery_db
    with session_scope(factory) as session:
        candidate_id = _burn_recovery_predecessor(session, ids, context, task_id)

    with factory() as session:
        candidate = session.get(release_models.ReleaseCandidate, candidate_id)
        assert candidate is not None
        setattr(candidate, field, value)
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="valid validation/approval/burn chronology"):
            promote_restored_generation(
                session,
                _control_for_candidate(context, ids, state, candidate),
                recovered_state=state,
                clock=lambda: WORKFLOW_NOW + timedelta(minutes=8),
            )


def test_cleanup_failure_retains_resources_and_fails_report(tmp_path):
    work_root = tmp_path / "work"
    data_dir = work_root / "cluster"
    data_dir.mkdir(parents=True)
    (data_dir / "postmaster.pid").write_text("43210\n", encoding="utf-8")

    def fail_stop():
        raise RuntimeError("server still running")

    cluster = SimpleNamespace(
        name="source",
        data_dir=data_dir,
        port=56520,
        stop=fail_stop,
    )
    report = {"status": "passed"}
    _cleanup_rehearsal(
        clusters=[cluster],
        evidence_dir=tmp_path / "evidence",
        work_root=work_root,
        keep_resources=False,
        report=report,
    )

    assert work_root.exists()
    assert report["status"] == "failed"
    assert any(
        item["result"] == "retained_due_to_cleanup_failure"
        for item in report["cleanup"]
    )
    assert any("pid=43210" in item for item in report["manual_cleanup"])



def test_main_returns_failure_when_cleanup_marks_report_failed(tmp_path, monkeypatch):
    report_path = tmp_path / "report.json"
    evidence_dir = tmp_path / "evidence"
    work_root = tmp_path / "work"

    monkeypatch.setattr(
        recovery_rehearsal,
        "_verified_source_identity",
        lambda _runner, _args: {"kind": "test"},
    )

    def fail_during_cleanup(_args, report, _runner):
        report["status"] = "failed"
        report["error"] = {
            "type": "RehearsalError",
            "message": "cleanup incomplete",
        }

    monkeypatch.setattr(recovery_rehearsal, "_run", fail_during_cleanup)
    result = recovery_rehearsal.main(
        [
            "--report",
            str(report_path),
            "--evidence-dir",
            str(evidence_dir),
            "--work-root",
            str(work_root),
        ]
    )

    assert result == 1
    assert report_path.exists()

def test_source_manifest_binds_runtime_dependencies():
    paths = {item["path"] for item in _source_manifest()["files"]}
    assert "requirements.txt" in paths
