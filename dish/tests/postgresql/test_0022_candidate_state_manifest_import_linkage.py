"""Typed import linkage is part of the approved candidate authority manifest."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dish_pg import import_link_models as import_links
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

        link = import_links.SourceImportNativeLink(
            link_id=_next(ids),
            evidence_id=evidence.evidence_id,
            import_batch_id=batch.import_batch_id,
            import_run_id=batch.import_run_id,
            entity_kind="task",
            project_id=None,
            section_id=None,
            task_id=task_id,
            content_version_id=None,
            request_tombstone_id=None,
            linked_at=NOW + timedelta(minutes=3),
        )
        session.add(link)
        session.flush()
        revalidation = _revalidate(session, ids, service, candidate_id)

        assert original_ids == (
            candidate.candidate_id,
            link.import_batch_id,
            link.import_run_id,
            link.evidence_id,
            link.task_id,
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
