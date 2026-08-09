"""Focused Agent B release-authority validation regressions."""
from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from dish_pg import import_link_models as import_links
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.release import EVIDENCE_ARTIFACT_KINDS, ReleaseAuthorityError
from dish_pg.repositories import RegistryRepository
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from tests.support.postgresql.release import (
    HASH_A,
    _independent_active_mapping_membership,
    _prepare_candidate,
    _record_final_closure,
)
from tests.support.postgresql.release_oracles import independent_sha256_json
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


def _independent_candidate_corpus_sha256(candidate, membership) -> str:
    return independent_sha256_json(
        {
            "contract": "release-active-projection-corpus-v1",
            "candidate_id": str(candidate.candidate_id),
            "generation_id": str(candidate.generation_id),
            "projection_epoch_id": str(candidate.projection_epoch_id),
            "items": [
                {"entity_kind": entity_kind, "mapping_id": str(mapping_id)}
                for entity_kind, mapping_id in sorted(
                    membership, key=lambda item: (item[0], str(item[1]))
                )
            ],
        }
    )


def test_candidate_reconciliation_writer_binds_canonical_authority_and_replays_safely(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        membership = _independent_active_mapping_membership(
            session, candidate=candidate
        )
        started_at = NOW + timedelta(minutes=1)
        run = service.start_candidate_reconciliation(
            candidate_id=candidate_id,
            corpus_identity="candidate-writer-production-path",
            observation_started_at=started_at,
            adapter_contract_version="asana-high-water-v1",
            started_at=started_at,
        )
        active_registry = session.get(
            models.ActiveSectionRegistry, candidate.generation_id
        )
        assert run.candidate_id == candidate_id
        assert run.registry_version_id == active_registry.registry_version_id
        assert run.expected_items == len(membership)
        assert run.processed_items == 0
        assert run.scope_complete is False
        assert run.status == "running"
        assert run.observation_started_at == started_at
        assert run.adapter_contract_version == "asana-high-water-v1"
        assert run.evidence_recorded_at == started_at
        assert run.corpus_manifest_sha256 == _independent_candidate_corpus_sha256(
            candidate, membership
        )

        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        for ordinal, (entity_kind, mapping_id) in enumerate(
            sorted(membership, key=lambda item: (item[0], str(item[1])))
        ):
            projection.record_reconciliation_item(
                reconciliation_run_id=run.reconciliation_run_id,
                item_identity=f"writer:{ordinal}:{entity_kind}:{mapping_id}",
                entity_kind=entity_kind,
                mapping_id=mapping_id,
                outcome="matched",
                evidence={"reread": "exact", "boundary": "asana-event-700"},
                recorded_at=started_at,
            )
        with pytest.raises(
            TransitionAuthorityError,
            match="candidate-bound reconciliation completion requires release authority",
        ):
            projection.complete_reconciliation(
                reconciliation_run_id=run.reconciliation_run_id,
                completed_at=started_at,
            )

        completed = service.complete_candidate_reconciliation(
            candidate_id=candidate_id,
            reconciliation_run_id=run.reconciliation_run_id,
            observation_completed_at=started_at,
            external_high_water="asana-event-700",
            completed_at=started_at,
        )
        assert completed.status == "complete"
        assert completed.scope_complete is True
        assert completed.observation_completed_at == started_at
        assert completed.evidence_recorded_at == started_at
        assert completed.external_high_water == "asana-event-700"
        assert completed.external_snapshot_identity is None

        replayed_start = service.start_candidate_reconciliation(
            candidate_id=candidate_id,
            corpus_identity="candidate-writer-production-path",
            observation_started_at=started_at,
            adapter_contract_version="asana-high-water-v1",
            started_at=started_at,
        )
        replayed_complete = service.complete_candidate_reconciliation(
            candidate_id=candidate_id,
            reconciliation_run_id=run.reconciliation_run_id,
            observation_completed_at=started_at,
            external_high_water="asana-event-700",
            completed_at=started_at,
        )
        assert replayed_start.reconciliation_run_id == run.reconciliation_run_id
        assert replayed_complete.reconciliation_run_id == run.reconciliation_run_id


def test_candidate_reconciliation_writer_rejects_wrong_run_and_registry_change(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        started_at = NOW + timedelta(minutes=1)

        generic = ProjectionService(
            session, uuid_factory=lambda: _next(ids)
        ).start_reconciliation(
            generation_id=candidate.generation_id,
            corpus_identity="generic-not-candidate-owned",
            expected_items=0,
            started_at=started_at,
        )
        with pytest.raises(
            ReleaseAuthorityError,
            match="does not belong to the release candidate",
        ):
            service.complete_candidate_reconciliation(
                candidate_id=candidate_id,
                reconciliation_run_id=generic.reconciliation_run_id,
                observation_completed_at=started_at,
                external_high_water="asana-event-generic",
                completed_at=started_at,
            )

        run = service.start_candidate_reconciliation(
            candidate_id=candidate_id,
            corpus_identity="candidate-registry-change",
            observation_started_at=started_at,
            adapter_contract_version="asana-high-water-v1",
            started_at=started_at,
        )
        active = session.get(models.ActiveSectionRegistry, candidate.generation_id)
        current = session.get(models.SectionRegistryVersion, active.registry_version_id)
        entries = session.scalars(
            select(models.SectionRegistryEntry).where(
                models.SectionRegistryEntry.registry_version_id
                == current.registry_version_id
            )
        ).all()
        replacement_id = _next(ids)
        replacement_activation_id = _next(ids)
        repo = RegistryRepository(session)
        repo.add_registry_version(
            models.SectionRegistryVersion(
                registry_version_id=replacement_id,
                generation_id=current.generation_id,
                version_number=current.version_number + 1,
                import_run_id=current.import_run_id,
                contract_binding_id=current.contract_binding_id,
                registry_sha256="b" * 64,
                created_at=started_at,
            ),
            [
                models.SectionRegistryEntry(
                    registry_version_id=replacement_id,
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
                registry_activation_id=replacement_activation_id,
                generation_id=current.generation_id,
                registry_version_id=replacement_id,
                activation_route="command_execution",
                import_run_id=None,
                command_execution_id=_next(ids),
                registry_revision=active.registry_revision + 1,
                activated_at=started_at,
            ),
            current=models.ActiveSectionRegistry(
                generation_id=current.generation_id,
                registry_version_id=replacement_id,
                registry_activation_id=replacement_activation_id,
                registry_revision=active.registry_revision + 1,
                updated_at=started_at,
            ),
        )
        with pytest.raises(
            ReleaseAuthorityError,
            match="not bound to the current candidate registry",
        ):
            service.complete_candidate_reconciliation(
                candidate_id=candidate_id,
                reconciliation_run_id=run.reconciliation_run_id,
                observation_completed_at=started_at,
                external_high_water="asana-event-701",
                completed_at=started_at,
            )


def test_candidate_reconciliation_writer_fails_closed_for_candidate_scope_and_epoch(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service._candidate(candidate_id)
        started_at = NOW + timedelta(minutes=1)
        run = service.start_candidate_reconciliation(
            candidate_id=candidate_id,
            corpus_identity="candidate-fail-closed",
            observation_started_at=started_at,
            adapter_contract_version="asana-high-water-v1",
            started_at=started_at,
        )

        with pytest.raises(ReleaseAuthorityError, match="unknown release candidate"):
            service.complete_candidate_reconciliation(
                candidate_id=_next(ids),
                reconciliation_run_id=run.reconciliation_run_id,
                observation_completed_at=started_at,
                external_high_water="asana-event-702",
                completed_at=started_at,
            )

        with pytest.raises(
            ReleaseAuthorityError, match="candidate reconciliation corpus is incomplete"
        ):
            service.complete_candidate_reconciliation(
                candidate_id=candidate_id,
                reconciliation_run_id=run.reconciliation_run_id,
                observation_completed_at=started_at,
                external_high_water="asana-event-702",
                completed_at=started_at,
            )

        ProjectionService(session, uuid_factory=lambda: _next(ids)).retire_epoch(
            projection_epoch_id=candidate.projection_epoch_id,
            retired_at=started_at,
        )
        with pytest.raises(
            ReleaseAuthorityError,
            match="candidate projection epoch to be active",
        ):
            service.complete_candidate_reconciliation(
                candidate_id=candidate_id,
                reconciliation_run_id=run.reconciliation_run_id,
                observation_completed_at=started_at,
                external_high_water="asana-event-702",
                completed_at=started_at,
            )


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
        # Synthetic corruption is intentional here: these branches prove the
        # validator fails closed against states ordinary production writers reject.
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
