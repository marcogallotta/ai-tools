from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.recovery_control import (
    RestoreControlError,
    promote_restored_generation,
)
from dish_pg import recovery_rehearsal
from dish_pg.recovery_rehearsal import _cleanup_rehearsal, _source_manifest
from tests.postgresql.test_recovery_control import (
    _control,
    core_db,
    _physical_state,
    _setup,
)
from tests.support.postgresql.core import NOW


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
