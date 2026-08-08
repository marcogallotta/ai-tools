from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

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
    _control,
    _physical_state,
    _setup,
)


def _activate_candidate(
    session, candidate, *, projection_epoch: uuid.UUID | None = None
) -> models.AuthorityActivation:
    approval = session.scalar(
        select(release_models.CutoverApproval).where(
            release_models.CutoverApproval.candidate_id == candidate.candidate_id
        )
    )
    batch = session.get(
        projection_models.SourceImportBatch, candidate.source_import_batch_id
    )
    assert approval is not None and batch is not None
    activation = models.AuthorityActivation(
        activation_id=uuid.uuid4(),
        generation_id=candidate.generation_id,
        import_run_id=batch.import_run_id,
        cutover_approval_id=str(approval.approval_id),
        legacy_bundle_id="section2-activated-candidate",
        schema_head=candidate.schema_head,
        dish_release=candidate.dish_release,
        honest_release=candidate.honest_release,
        protocol_release=candidate.protocol_release,
        openapi_release=candidate.openapi_release,
        routing_release=candidate.routing_release,
        projection_epoch=(
            candidate.projection_epoch_id if projection_epoch is None else projection_epoch
        ),
        outcome="activated",
        rollback_burned_at=NOW,
        recorded_at=NOW,
    )
    candidate.status = "activated"
    candidate.candidate_revision += 1
    candidate.terminal_at = NOW
    session.add(activation)
    session.commit()
    return activation


def test_activated_candidate_requires_exact_activation_lineage(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, candidate, projection_epoch=uuid.uuid4())
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
        _activate_candidate(session, candidate)
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
        _activate_candidate(session, candidate)
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


def test_recovery_rejects_active_registry_drift_from_approval_identity(core_db):
    factory, ids = core_db
    with factory() as session:
        context, _, candidate = _setup(session, ids, candidate_status="approved")
        _activate_candidate(session, candidate)
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
                registry_sha256=source.registry_sha256,
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
        with pytest.raises(RestoreControlError, match="historical identity is inconsistent"):
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
