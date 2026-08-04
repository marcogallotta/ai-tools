"""Release/cutover builders shared by PostgreSQL safety tests."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, text

from dish_pg import stage5_models as tx
from dish_pg.release import ALEMBIC_HEAD, ReleaseCandidateService
from dish_pg.transition import ProjectionService, ShadowService, SourceImportService
from tests.support.postgresql.release_oracles import (
    EXPECTED_EVIDENCE_ARTIFACT_KINDS,
    EXPECTED_RELEASE_EVIDENCE,
    EXPECTED_REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    EXPECTED_REHEARSAL_CHECKPOINTS,
    EXPECTED_REHEARSALS,
    independent_sha256_json,
)
from tests.support.postgresql.workflow import NOW, _next

ROOT = Path(__file__).resolve().parents[3]
HASH_A = "a" * 64


def _prepare_candidate(
    session,
    ids,
    context,
    task_id,
    *,
    evidence_contracts=EXPECTED_RELEASE_EVIDENCE,
    rehearsal_kinds=EXPECTED_REHEARSALS,
    rehearsal_checkpoints=EXPECTED_REHEARSAL_CHECKPOINTS,
):
    # Base.metadata fixtures do not normally carry Alembic's version table.
    session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(64) NOT NULL)"))
    session.execute(text("DELETE FROM alembic_version"))
    session.execute(text("INSERT INTO alembic_version(version_num) VALUES (:head)"), {"head": ALEMBIC_HEAD})

    source = SourceImportService(session, uuid_factory=lambda: _next(ids))
    batch_id = _next(ids)
    source.start_batch(
        import_batch_id=batch_id,
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        source_release="dish-42619b9",
        source_commit="42619b9",
        source_database_sha256=HASH_A,
        source_sidecars={"audit": {"sha256": HASH_A}},
        ledger_through_commit="42619b9",
        expected_entities=1,
        started_at=NOW,
    )
    source.record_entity(
        import_batch_id=batch_id,
        entity_kind="task",
        source_identity="asana:123456789",
        source_sha256=HASH_A,
        target_entity_type="dish_task",
        target_entity_id=task_id,
        provenance={"source": "stage6-fixture"},
        imported_at=NOW,
    )
    source.complete_batch(import_batch_id=batch_id, completed_at=NOW)

    shadow = ShadowService(session, uuid_factory=lambda: _next(ids))
    baseline = shadow.create_baseline(
        generation_id=context["generation_id"],
        source_generation_identity="legacy-generation-1",
        source_commit="42619b9",
        created_at=NOW,
    )
    shadow.close_baseline(baseline_id=baseline.shadow_baseline_id, closed_at=NOW)

    projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
    epoch = projection.activate_epoch(
        generation_id=context["generation_id"], activation_reason="stage6 rehearsal", created_at=NOW,
        external_effects_enabled=True,
    )
    assert projection.bind_imported_mappings(
        generation_id=context["generation_id"], bound_at=NOW
    ) == (1, 1, 1)
    mappings = []
    for mapping_model, kind in (
        (tx.ProjectProjectionMapping, "project"),
        (tx.SectionProjectionMapping, "section"),
        (tx.TaskProjectionMapping, "task"),
    ):
        mapping = session.scalar(
            select(mapping_model).where(
                mapping_model.generation_id == context["generation_id"],
                mapping_model.projection_epoch_id == epoch.projection_epoch_id,
                mapping_model.state == "active",
            )
        )
        mappings.append((mapping, kind))
    reconciliation = projection.start_reconciliation(
        generation_id=context["generation_id"],
        corpus_identity="production-corpus@42619b9",
        expected_items=len(mappings),
        started_at=NOW,
    )
    for mapping, kind in mappings:
        projection.record_reconciliation_item(
            reconciliation_run_id=reconciliation.reconciliation_run_id,
            item_identity=f"{kind}:{mapping.mapping_id}",
            entity_kind=kind,
            mapping_id=mapping.mapping_id,
            outcome="matched",
            evidence={"reread": "exact"},
            recorded_at=NOW,
        )
    projection.complete_reconciliation(
        reconciliation_run_id=reconciliation.reconciliation_run_id, completed_at=NOW
    )

    candidate_id = _next(ids)
    service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
    service.create_candidate(
        candidate_id=candidate_id,
        generation_id=context["generation_id"],
        source_import_batch_id=batch_id,
        shadow_baseline_id=baseline.shadow_baseline_id,
        projection_epoch_id=epoch.projection_epoch_id,
        source_release="dish-42619b9",
        source_commit="42619b9",
        ledger_through_commit="42619b9",
        schema_head=ALEMBIC_HEAD,
        dish_release="dish-pg-stage6",
        honest_release="honest-1",
        protocol_release="protocol-1",
        openapi_release="openapi-stage4",
        routing_release="routing-stage6",
        created_at=NOW,
    )
    for category, key in evidence_contracts:
        service.record_evidence(
            candidate_id=candidate_id,
            category=category,
            evidence_key=key,
            outcome="pass",
            payload={
                "artifact_kind": EXPECTED_EVIDENCE_ARTIFACT_KINDS[(category, key)],
                "artifact_identity": f"fixture:{category}:{key}",
                "artifact_path": f"/evidence/{category}/{key}.json",
                "artifact_sha256": HASH_A,
                "source_manifest_sha256": HASH_A,
                "gate_name": f"{category}:{key}",
                "gate_result": "pass",
            },
            recorded_at=NOW,
        )
    for kind in rehearsal_kinds:
        rehearsal = service.start_rehearsal(
            candidate_id=candidate_id,
            rehearsal_kind=kind,
            environment_identity="production-shaped-fixture",
            source_manifest_sha256=HASH_A,
            started_at=NOW,
        )
        checkpoints = []
        for checkpoint_kind in rehearsal_checkpoints[kind]:
            checkpoint = service.record_rehearsal_checkpoint(
                rehearsal_id=rehearsal.rehearsal_id,
                checkpoint_kind=checkpoint_kind,
                payload={
                    "rehearsal_kind": kind,
                    "checkpoint_kind": checkpoint_kind,
                    "evidence_kind": EXPECTED_REHEARSAL_CHECKPOINT_EVIDENCE_KINDS[kind][checkpoint_kind],
                    "artifact_identity": f"fixture:{kind}:{checkpoint_kind}",
                    "artifact_sha256": HASH_A,
                    "source_manifest_sha256": HASH_A,
                    "gate_result": "pass",
                },
                recorded_at=NOW,
            )
            checkpoints.append(
                {
                    "checkpoint_kind": checkpoint.checkpoint_kind,
                    "payload_sha256": checkpoint.payload_sha256,
                }
            )
        service.finish_rehearsal(
            rehearsal_id=rehearsal.rehearsal_id,
            passed=True,
            report={
                "rehearsal_kind": kind,
                "source_manifest_sha256": HASH_A,
                "result": "passed",
                "checkpoint_manifest_sha256": independent_sha256_json(checkpoints),
            },
            measured_rpo_seconds=0.0 if kind == "restore" else None,
            measured_rto_seconds=12.5 if kind == "restore" else None,
            completed_at=NOW,
        )
    return service, candidate_id


def _record_final_closure(service, ids, candidate_id, *, closed_through_at):
    return service.record_final_asana_closure(
        candidate_id=candidate_id,
        capture_manifest_sha256=HASH_A,
        observation_high_water="asana-change-900",
        watcher_identity="final-asana-watcher@fixture",
        interval_started_at=NOW,
        closed_through_at=closed_through_at,
        payload={"tasks": 1, "registry": "closed"},
        recorded_at=closed_through_at,
    )


def _writer_fence_proof(fence, candidate_id):
    return {
        "probe_kind": "authenticated_mutation_rejected_before_body_parse",
        "candidate_id": str(candidate_id),
        "target_identity": fence.target_identity,
        "fence_manifest_sha256": fence.manifest_sha256,
        "request_token_sha256": "f" * 64,
        "http_status": 409,
        "response_code": "CONFLICT",
        "response_rule": "legacy_writer_fenced",
        "response_retryable": False,
        "body_loaded": False,
        "result": "pass",
    }


def _complete_active_mapping_reconciliation(
    session,
    ids,
    *,
    generation_id,
    corpus_identity: str,
    started_at,
    completed_at,
):
    active_mappings: list[tuple[str, object]] = []
    for mapping_model, entity_kind in (
        (tx.ProjectProjectionMapping, "project"),
        (tx.SectionProjectionMapping, "section"),
        (tx.TaskProjectionMapping, "task"),
    ):
        rows = session.scalars(
            select(mapping_model).where(
                mapping_model.generation_id == generation_id,
                mapping_model.state == "active",
            )
        ).all()
        active_mappings.extend((entity_kind, row) for row in rows)

    projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
    run = projection.start_reconciliation(
        generation_id=generation_id,
        corpus_identity=corpus_identity,
        expected_items=len(active_mappings),
        started_at=started_at,
    )
    for entity_kind, mapping in active_mappings:
        projection.record_reconciliation_item(
            reconciliation_run_id=run.reconciliation_run_id,
            item_identity=f"{entity_kind}:{mapping.mapping_id}",
            entity_kind=entity_kind,
            mapping_id=mapping.mapping_id,
            outcome="matched",
            evidence={"source": corpus_identity},
            recorded_at=started_at,
        )
    projection.complete_reconciliation(
        reconciliation_run_id=run.reconciliation_run_id,
        completed_at=completed_at,
    )
    return run
