"""Focused Agent B release-authority validation regressions."""
from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from dish_pg import import_link_models as import_links
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import EVIDENCE_ARTIFACT_KINDS, ReleaseAuthorityError
from tests.support.postgresql.release import (
    HASH_A,
    _prepare_candidate,
    _record_final_closure,
)
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _check(evaluation, code: str):
    return next(item for item in evaluation.checks if item.code == code)


def _evidence_payload(path: Path, *, identity: str) -> dict[str, object]:
    return {
        "artifact_kind": EVIDENCE_ARTIFACT_KINDS[("authority_coverage", "current_to_target")],
        "artifact_identity": identity,
        "artifact_path": str(path),
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_manifest_sha256": HASH_A,
        "gate_name": "authority_coverage:current_to_target",
        "gate_result": "pass",
    }


def test_release_artifact_substitution_is_detected_during_revalidation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        item = session.scalar(
            select(rel.ReleaseEvidenceItem).where(
                rel.ReleaseEvidenceItem.candidate_id == candidate_id
            ).limit(1)
        )
        path = Path(item.payload["artifact_path"])
        path.write_bytes(b"substituted-after-recording\n")

        evaluation = service.evaluate_candidate(candidate_id=candidate_id)
        check = _check(evaluation, "required_acceptance_evidence")
        assert not check.passed
        assert check.details["artifact_errors"]
        assert "digest does not match" in next(iter(check.details["artifact_errors"].values()))


def test_release_artifact_symlink_and_stale_file_fail_closed(workflow_db, tmp_path: Path) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        target = tmp_path / "actual.json"
        target.write_bytes(b"actual\n")
        link = tmp_path / "evidence.json"
        link.symlink_to(target)
        with pytest.raises(ReleaseAuthorityError, match="forbidden symlink"):
            service.record_evidence(
                candidate_id=candidate_id,
                category="authority_coverage",
                evidence_key="current_to_target",
                outcome="pass",
                payload={
                    **_evidence_payload(target, identity="fixture:symlink"),
                    "artifact_path": str(link),
                },
                recorded_at=NOW + timedelta(minutes=1),
            )

        stale = tmp_path / "stale.json"
        stale.write_bytes(b"stale\n")
        stale_time = (NOW - timedelta(hours=1)).timestamp()
        os.utime(stale, (stale_time, stale_time))
        with pytest.raises(ReleaseAuthorityError, match="predates the candidate"):
            service.record_evidence(
                candidate_id=candidate_id,
                category="authority_coverage",
                evidence_key="current_to_target",
                outcome="pass",
                payload=_evidence_payload(stale, identity="fixture:stale"),
                recorded_at=NOW + timedelta(minutes=1),
            )
        assert target.read_bytes() == b"actual\n"


def test_typed_import_requires_every_source_evidence_to_have_exact_native_link(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        link = session.scalar(
            select(import_links.SourceImportNativeLink).where(
                import_links.SourceImportNativeLink.import_batch_id
                == candidate.source_import_batch_id,
                import_links.SourceImportNativeLink.entity_kind == "task",
            )
        )
        evidence_id = link.evidence_id
        session.delete(link)
        session.flush()

        check = _check(
            service.evaluate_candidate(candidate_id=candidate_id),
            "typed_import_linkage_exact",
        )
        assert not check.passed
        assert check.details["missing_typed_links"] == [f"task:{task_id}"]
        assert check.details["unlinked_evidence_ids"] == [str(evidence_id)]


@pytest.mark.parametrize(
    "defect",
    ("candidate_binding", "scope", "corpus_digest", "external_boundary", "freshness"),
)
def test_reconciliation_requires_exact_candidate_scope_digest_boundary_and_freshness(
    workflow_db, defect: str
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        run = session.scalar(
            select(tx.ProjectionReconciliationRun).where(
                tx.ProjectionReconciliationRun.candidate_id == candidate_id
            )
        )
        evaluation_at = NOW
        if defect == "candidate_binding":
            run.candidate_id = None
            run.registry_version_id = None
            run.observation_started_at = None
            run.observation_completed_at = None
            run.external_snapshot_identity = None
            run.external_high_water = None
            run.corpus_manifest_sha256 = None
            run.scope_complete = None
            run.adapter_contract_version = None
            run.evidence_recorded_at = None
        elif defect == "scope":
            run.status = "running"
            run.scope_complete = False
            run.completed_at = None
        elif defect == "corpus_digest":
            run.corpus_manifest_sha256 = "f" * 64
        elif defect == "external_boundary":
            run.external_snapshot_identity = "snapshot-and-high-water"
        elif defect == "freshness":
            evaluation_at = NOW + timedelta(hours=2)
        session.flush()

        check = _check(
            service.evaluate_candidate(candidate_id=candidate_id, as_of=evaluation_at),
            "projection_ready",
        )
        assert not check.passed
        if defect == "candidate_binding":
            assert not check.details["candidate_bound"]
        elif defect == "scope":
            assert check.details["scope_complete"] is False
        elif defect == "corpus_digest":
            assert (
                check.details["corpus_manifest_observed"]
                != check.details["corpus_manifest_expected"]
            )
        elif defect == "external_boundary":
            assert not check.details["external_boundary_supported"]
        elif defect == "freshness":
            assert not check.details["fresh"]


def test_approval_revalidates_reconciliation_freshness(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=NOW,
        )
        service.validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            validated_at=NOW + timedelta(minutes=1),
        )
        closure = _record_final_closure(
            service,
            ids,
            candidate_id,
            closed_through_at=NOW + timedelta(hours=2),
        )
        with pytest.raises(ReleaseAuthorityError, match="no longer satisfied at approval"):
            service.approve_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=bundle.bundle_id,
                approver="Marco",
                approval_statement="This stale reconciliation must not approve.",
                approval_payload={
                    "final_asana_closure_id": str(closure.closure_id),
                    "final_asana_closure_sha256": closure.closure_sha256,
                },
                approved_at=NOW + timedelta(hours=2),
            )
        assert session.get(rel.ReleaseCandidate, candidate_id).status == "validated"


def test_quiescent_gate_rejects_open_authority_and_ambiguous_projection(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        session.add(
            tx.ProjectionOutboxEvent(
                projection_event_id=_next(ids),
                generation_id=candidate.generation_id,
                projection_epoch_id=candidate.projection_epoch_id,
                source_route="service",
                origin="live",
                command_execution_id=None,
                task_id=task_id,
                event_type="reproject",
                aggregate_sequence=1,
                idempotency_key="d" * 64,
                intent_payload={"reason": "ambiguous-cutover-regression"},
                intent_sha256="e" * 64,
                state="uncertain",
                claim_owner=None,
                claim_token=None,
                claim_expires_at=None,
                outbox_revision=1,
                created_at=NOW,
                terminal_at=NOW,
            )
        )
        session.flush()
        check = _check(
            service.evaluate_candidate(candidate_id=candidate_id),
            "quiescent_cutover_authority",
        )
        assert not check.passed
        assert check.details["projection_outbox"] == 1
