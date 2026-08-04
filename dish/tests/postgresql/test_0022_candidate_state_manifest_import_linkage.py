"""Typed import linkage is part of the approved candidate authority manifest."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dish_pg import import_link_models as import_links
from dish_pg import legacy_request_models as legacy
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from tests.support.postgresql.candidate_manifest import (
    _approve,
    _revalidate,
    _validated_candidate,
)
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def test_0022_typed_import_link_change_under_same_ids_revalidates_stale(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id, bundle, closure = _validated_candidate(
            session, ids, context, task_id
        )
        candidate = service._candidate(candidate_id)
        evidence = session.scalar(
            select(tx.SourceImportEntityEvidence).where(
                tx.SourceImportEntityEvidence.import_batch_id
                == candidate.source_import_batch_id,
                tx.SourceImportEntityEvidence.entity_kind == "task",
            )
        )
        batch = session.get(tx.SourceImportBatch, candidate.source_import_batch_id)
        assert evidence is not None and batch is not None
        manifest = _approve(session, service, candidate_id, bundle, closure)
        original_ids = (
            candidate.candidate_id,
            batch.import_batch_id,
            batch.import_run_id,
            evidence.evidence_id,
            task_id,
        )

        tombstone = legacy.LegacyRequestTombstone(
            tombstone_id=_next(ids),
            request_id=_next(ids),
            source_authority="legacy",
            import_run_id=batch.import_run_id,
            import_batch_id=batch.import_batch_id,
            source_identity_sha256="b" * 64,
            source_metadata={"source": "manifest-drift-test"},
            imported_at=NOW + timedelta(minutes=3),
        )
        session.add(tombstone)
        session.flush()
        new_evidence = tx.SourceImportEntityEvidence(
            evidence_id=_next(ids),
            import_batch_id=batch.import_batch_id,
            entity_kind="request_tombstone",
            source_identity=f"legacy-request:{tombstone.request_id}",
            source_sha256="b" * 64,
            target_entity_type="legacy_request_tombstone",
            target_entity_id=tombstone.tombstone_id,
            provenance={"source": "manifest-drift-test"},
            imported_at=NOW + timedelta(minutes=3),
        )
        session.add(new_evidence)
        session.flush()
        link = import_links.SourceImportNativeLink(
            link_id=_next(ids),
            evidence_id=new_evidence.evidence_id,
            import_batch_id=batch.import_batch_id,
            import_run_id=batch.import_run_id,
            entity_kind="request_tombstone",
            project_id=None,
            section_id=None,
            task_id=None,
            content_version_id=None,
            request_tombstone_id=tombstone.tombstone_id,
            linked_at=NOW + timedelta(minutes=3),
        )
        session.add(link)
        session.flush()
        revalidation = _revalidate(session, ids, service, candidate_id)

        assert original_ids[:3] == (
            candidate.candidate_id,
            link.import_batch_id,
            link.import_run_id,
        )
        assert revalidation.result == "stale"
        assert (
            revalidation.observed_typed_import_linkage_sha256
            != manifest.typed_import_linkage_sha256
        )
        assert (
            revalidation.observed_import_completion_sha256
            == manifest.import_completion_sha256
        )
