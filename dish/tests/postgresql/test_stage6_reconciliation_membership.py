from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dish_pg import stage5_models as tx
from dish_pg import stage6_models as rel
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _active_mappings(session, *, generation_id, projection_epoch_id):
    rows = []
    for entity_kind, mapping_model in (
        ("project", tx.ProjectProjectionMapping),
        ("section", tx.SectionProjectionMapping),
        ("task", tx.TaskProjectionMapping),
    ):
        mapping = session.scalar(
            select(mapping_model).where(
                mapping_model.generation_id == generation_id,
                mapping_model.projection_epoch_id == projection_epoch_id,
                mapping_model.state == "active",
            )
        )
        assert mapping is not None
        rows.append((entity_kind, mapping.mapping_id))
    return rows


def _record_run(
    session,
    ids,
    *,
    generation_id,
    expected_items,
    items,
    started_at,
    corpus_identity,
):
    projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
    run = projection.start_reconciliation(
        generation_id=generation_id,
        corpus_identity=corpus_identity,
        expected_items=expected_items,
        started_at=started_at,
    )
    for ordinal, (entity_kind, mapping_id) in enumerate(items):
        projection.record_reconciliation_item(
            reconciliation_run_id=run.reconciliation_run_id,
            item_identity=f"{ordinal}:{entity_kind}:{mapping_id}",
            entity_kind=entity_kind,
            mapping_id=mapping_id,
            outcome="matched",
            evidence={"corpus": corpus_identity},
            recorded_at=started_at,
        )
    projection.complete_reconciliation(
        reconciliation_run_id=run.reconciliation_run_id,
        completed_at=started_at,
    )
    return run


def _projection_check(service, candidate_id):
    return next(
        check
        for check in service.evaluate_candidate(candidate_id=candidate_id).checks
        if check.code == "projection_ready"
    )


def test_release_requires_exact_active_reconciliation_mapping_membership(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        required = _active_mappings(
            session,
            generation_id=context["generation_id"],
            projection_epoch_id=candidate.projection_epoch_id,
        )

        exact = _projection_check(service, candidate_id)
        assert exact.passed
        assert exact.details["active_mappings"] == 3
        assert exact.details["reconciled_mappings"] == 3
        assert len(exact.details["required_project_mapping_ids"]) == 1
        assert len(exact.details["required_section_mapping_ids"]) == 1
        assert len(exact.details["required_task_mapping_ids"]) == 1
        assert exact.details["missing_mapping_membership"] == []
        assert exact.details["extra_mapping_membership"] == []

        fabricated = [
            (entity_kind, _next(ids)) for entity_kind, _mapping_id in required
        ]
        _record_run(
            session,
            ids,
            generation_id=context["generation_id"],
            expected_items=len(fabricated),
            items=fabricated,
            started_at=NOW + timedelta(minutes=1),
            corpus_identity="same-count-fabricated-identities",
        )
        same_count = _projection_check(service, candidate_id)
        assert not same_count.passed
        assert same_count.details["active_mappings"] == 3
        assert same_count.details["reconciled_mappings"] == 3
        assert len(same_count.details["missing_mapping_membership"]) == 3
        assert len(same_count.details["extra_mapping_membership"]) == 3
        assert same_count.details["invalid_reconciliation_rows"] == 3

        missing_items = required[:-1]
        _record_run(
            session,
            ids,
            generation_id=context["generation_id"],
            expected_items=len(missing_items),
            items=missing_items,
            started_at=NOW + timedelta(minutes=2),
            corpus_identity="missing-one-required-identity",
        )
        missing = _projection_check(service, candidate_id)
        assert not missing.passed
        assert len(missing.details["missing_mapping_membership"]) == 1
        assert missing.details["extra_mapping_membership"] == []

        extra_items = required + [("task", _next(ids))]
        _record_run(
            session,
            ids,
            generation_id=context["generation_id"],
            expected_items=len(extra_items),
            items=extra_items,
            started_at=NOW + timedelta(minutes=3),
            corpus_identity="required-identities-plus-extra",
        )
        extra = _projection_check(service, candidate_id)
        assert not extra.passed
        assert extra.details["missing_mapping_membership"] == []
        assert len(extra.details["extra_mapping_membership"]) == 1
        assert extra.details["invalid_reconciliation_rows"] == 1


def test_empty_active_mapping_corpus_cannot_vacuously_pass(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        projection.retire_epoch(
            projection_epoch_id=candidate.projection_epoch_id,
            retired_at=NOW + timedelta(minutes=1),
        )
        empty_epoch = projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="empty-corpus-regression",
            external_effects_enabled=True,
            created_at=NOW + timedelta(minutes=2),
        )
        candidate.projection_epoch_id = empty_epoch.projection_epoch_id
        _record_run(
            session,
            ids,
            generation_id=context["generation_id"],
            expected_items=0,
            items=[],
            started_at=NOW + timedelta(minutes=3),
            corpus_identity="empty-active-corpus",
        )

        projection_check = _projection_check(service, candidate_id)
        assert not projection_check.passed
        assert projection_check.details["active_mappings"] == 0
        assert projection_check.details["reconciled_mappings"] == 0
        assert projection_check.details["reconciliation_expected"] == 0
        assert projection_check.details["reconciliation_processed"] == 0
