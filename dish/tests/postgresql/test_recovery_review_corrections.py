from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.candidate_manifest import revalidate_candidate_manifest
from dish_pg.recovery_control import (
    RestoreControlError,
    promote_restored_generation,
)
from dish_pg.repositories import RegistryRepository
from dish_pg import recovery_rehearsal
from dish_pg.recovery_rehearsal import _cleanup_rehearsal, _source_manifest
from tests.support.postgresql.core import NOW, core_db
from tests.support.postgresql.recovery_control import (
    _activate_candidate,
    _control,
    _physical_state,
    _setup,
)



def test_activated_candidate_requires_exact_activation_lineage(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        activation = _activate_candidate(session, ids, candidate)
        activation.projection_epoch = uuid.uuid4()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="exact activation evidence"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )


def test_exact_activated_candidate_can_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        state = _physical_state()
        result = promote_restored_generation(
            session,
            _control(context, ids, state),
            recovered_state=state,
            clock=lambda: NOW + timedelta(minutes=2),
        )
        assert result.predecessor_generation_id == context["generation_id"]


def test_activated_candidate_recovery_authority_survives_runtime_mapping_evolution(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        project = session.scalar(select(models.GovernedProject))
        alias = session.scalar(select(models.ProjectExternalAlias))
        assert project is not None and alias is not None
        session.add(
            projection_models.ProjectProjectionMapping(
                mapping_id=next(ids),
                generation_id=candidate.generation_id,
                projection_epoch_id=candidate.projection_epoch_id,
                project_id=project.project_id,
                alias_id=alias.alias_id,
                state="active",
                mapping_revision=1,
                bound_at=NOW + timedelta(seconds=1),
                retired_at=None,
            )
        )
        session.commit()
        revalidation = revalidate_candidate_manifest(
            session,
            uuid_factory=lambda: next(ids),
            candidate=candidate,
            revalidated_at=NOW + timedelta(seconds=2),
        )
        assert revalidation.result == "stale"
        session.commit()

        state = _physical_state()
        result = promote_restored_generation(
            session,
            _control(context, ids, state),
            recovered_state=state,
            clock=lambda: NOW + timedelta(minutes=2),
        )

        assert result.predecessor_generation_id == context["generation_id"]


def test_recovery_accepts_current_coherent_registry_evolution_and_clones_it(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        active = session.get(models.ActiveSectionRegistry, candidate.generation_id)
        assert active is not None
        source = session.get(models.SectionRegistryVersion, active.registry_version_id)
        assert source is not None
        entries = session.scalars(
            select(models.SectionRegistryEntry).where(
                models.SectionRegistryEntry.registry_version_id == source.registry_version_id
            )
        ).all()
        version_id = next(ids)
        activation_id = next(ids)
        repo = RegistryRepository(session)
        repo.add_registry_version(
            models.SectionRegistryVersion(
                registry_version_id=version_id,
                generation_id=candidate.generation_id,
                version_number=source.version_number + 1,
                import_run_id=source.import_run_id,
                contract_binding_id=source.contract_binding_id,
                registry_sha256="d" * 64,
                created_at=NOW + timedelta(seconds=1),
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
                generation_id=candidate.generation_id,
                registry_version_id=version_id,
                activation_route="import",
                import_run_id=source.import_run_id,
                command_execution_id=None,
                registry_revision=active.registry_revision + 1,
                activated_at=NOW + timedelta(seconds=1),
            ),
            current=models.ActiveSectionRegistry(
                generation_id=candidate.generation_id,
                registry_version_id=version_id,
                registry_activation_id=activation_id,
                registry_revision=active.registry_revision + 1,
                updated_at=NOW + timedelta(seconds=1),
            ),
        )
        session.commit()

        state = _physical_state()
        result = promote_restored_generation(
            session,
            _control(context, ids, state),
            recovered_state=state,
            clock=lambda: NOW + timedelta(minutes=2),
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
def test_burned_candidate_requires_exact_activation_authority(core_db, mutation, message):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        activation = _activate_candidate(session, ids, candidate)
        mutation(activation)
        state = _physical_state()
        with pytest.raises(RestoreControlError, match=message):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )


def test_corrupt_release_candidate_evidence_bundle_cannot_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        bundle = session.scalar(
            select(release_models.EvidenceBundle).where(
                release_models.EvidenceBundle.candidate_id == candidate.candidate_id,
                release_models.EvidenceBundle.bundle_kind == "release_candidate",
            )
        )
        assert bundle is not None
        bundle.manifest = {"candidate_id": str(candidate.candidate_id), "authorized": False}
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="validation bundle is corrupt"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )


def test_broken_approval_manifest_identity_bridge_cannot_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        binding = session.scalar(
            select(manifest_models.CutoverApprovalManifestBinding).where(
                manifest_models.CutoverApprovalManifestBinding.candidate_id
                == candidate.candidate_id
            )
        )
        assert binding is not None
        binding.candidate_id = uuid.uuid4()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="manifest binding is inconsistent"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )


def test_broken_manifest_source_import_identity_bridge_cannot_promote(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        manifest = session.scalar(
            select(manifest_models.ReleaseCandidateManifest).where(
                manifest_models.ReleaseCandidateManifest.candidate_id
                == candidate.candidate_id
            )
        )
        assert manifest is not None
        manifest.source_import_batch_id = uuid.uuid4()
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="manifest binding is inconsistent"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("validated_at", NOW + timedelta(seconds=1)),
        ("approved_at", NOW + timedelta(seconds=1)),
    ],
    ids=["validation-after-approval", "approval-after-burn"],
)
def test_invalid_candidate_authority_chronology_cannot_promote(core_db, field, value):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, ids, candidate)
        setattr(candidate, field, value)
        state = _physical_state()
        with pytest.raises(RestoreControlError, match="valid validation/approval/burn chronology"):
            promote_restored_generation(
                session,
                _control(context, ids, state),
                recovered_state=state,
                clock=lambda: NOW + timedelta(minutes=2),
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
