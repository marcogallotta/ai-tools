"""Release/cutover builders shared by PostgreSQL safety tests."""
from __future__ import annotations

from pathlib import Path
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from dish_pg import import_link_models as import_links
from dish_pg import models
from dish_pg import readiness_evidence_models as typed_readiness
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


def _independent_active_mapping_membership(session, *, candidate):
    membership = set()
    for entity_kind, model in (
        ("project", tx.ProjectProjectionMapping),
        ("section", tx.SectionProjectionMapping),
        ("task", tx.TaskProjectionMapping),
    ):
        mapping_ids = session.scalars(
            select(model.mapping_id).where(
                model.generation_id == candidate.generation_id,
                model.projection_epoch_id == candidate.projection_epoch_id,
                model.state == "active",
            )
        ).all()
        membership.update((entity_kind, mapping_id) for mapping_id in mapping_ids)
    return membership


def _independent_reconciliation_corpus_sha256(*, candidate, membership) -> str:
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


def _independent_iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _independent_worker_inventory_sha256(*, inventory, requirements) -> str:
    return independent_sha256_json(
        {
            "contract": "worker-probe-inventory-v1",
            "candidate_id": str(inventory.candidate_id),
            "projection_epoch_id": str(inventory.projection_epoch_id),
            "inventory_version": inventory.inventory_version,
            "inventory_contract_version": inventory.inventory_contract_version,
            "requirements": [
                {
                    "probe_kind": item.probe_kind,
                    "ordinal": item.ordinal,
                    "probe_contract_version": item.probe_contract_version,
                }
                for item in sorted(requirements, key=lambda row: row.ordinal)
            ],
        }
    )


def _independent_worker_probe_evidence_sha256(
    *, readiness, requirement, inventory, deployed_artifact_sha256, observed_at
) -> str:
    return independent_sha256_json(
        {
            "contract": "worker-probe-evidence-v1",
            "readiness_id": str(readiness.readiness_id),
            "requirement_id": str(requirement.requirement_id),
            "inventory_id": str(inventory.inventory_id),
            "candidate_id": str(inventory.candidate_id),
            "projection_epoch_id": str(inventory.projection_epoch_id),
            "probe_kind": requirement.probe_kind,
            "execution_identity": (
                f"probe:{requirement.probe_kind}:{readiness.readiness_id}"
            ),
            "worker_identity": readiness.worker_identity,
            "deployed_artifact_sha256": deployed_artifact_sha256,
            "result": "pass",
            "observed_at": _independent_iso_utc(observed_at),
            "evidence_artifact_identity": f"fixture:{requirement.probe_kind}",
        }
    )


def _independent_worker_completion_sha256(
    *, readiness, inventory, completed_at
) -> str:
    return independent_sha256_json(
        {
            "contract": "worker-readiness-completion-v1",
            "readiness_id": str(readiness.readiness_id),
            "inventory_id": str(inventory.inventory_id),
            "candidate_id": str(inventory.candidate_id),
            "projection_epoch_id": str(inventory.projection_epoch_id),
            "completion_state": "complete",
            "required_probe_count": inventory.required_probe_count,
            "passed_probe_count": inventory.required_probe_count,
            "completed_at": _independent_iso_utc(completed_at),
        }
    )


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
        expected_entities=4,
        started_at=NOW,
    )
    content_version_id = session.scalar(
        select(models.ContentVersion.content_version_id).where(
            models.ContentVersion.task_id == task_id,
            models.ContentVersion.import_run_id == context["import_run_id"],
        )
    )
    imported_targets = (
        ("project", "governed_project", context["project_id"]),
        ("section", "governed_section", context["section_id"]),
        ("task", "dish_task", task_id),
        ("content", "task_content_version", content_version_id),
    )
    evidence_by_kind = {}
    for entity_kind, target_type, target_id in imported_targets:
        evidence_by_kind[entity_kind] = source.record_entity(
            import_batch_id=batch_id,
            entity_kind=entity_kind,
            source_identity=f"fixture:{entity_kind}:{target_id}",
            source_sha256=HASH_A,
            target_entity_type=target_type,
            target_entity_id=target_id,
            provenance={"source": "stage6-fixture"},
            imported_at=NOW,
        )
    source.complete_batch(import_batch_id=batch_id, completed_at=NOW)
    for entity_kind, _target_type, target_id in imported_targets:
        values = {
            "project_id": None,
            "section_id": None,
            "task_id": None,
            "content_version_id": None,
            "request_tombstone_id": None,
        }
        values[{
            "project": "project_id",
            "section": "section_id",
            "task": "task_id",
            "content": "content_version_id",
        }[entity_kind]] = target_id
        session.add(import_links.SourceImportNativeLink(
            link_id=_next(ids),
            evidence_id=evidence_by_kind[entity_kind].evidence_id,
            import_batch_id=batch_id,
            import_run_id=context["import_run_id"],
            entity_kind=entity_kind,
            linked_at=NOW,
            **values,
        ))
    session.flush()

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
    candidate = service._candidate(candidate_id)
    active_registry = session.get(models.ActiveSectionRegistry, context["generation_id"])
    membership = _independent_active_mapping_membership(session, candidate=candidate)
    reconciliation = tx.ProjectionReconciliationRun(
        reconciliation_run_id=_next(ids),
        generation_id=candidate.generation_id,
        projection_epoch_id=candidate.projection_epoch_id,
        corpus_identity="candidate-release-corpus@42619b9",
        candidate_id=candidate_id,
        registry_version_id=active_registry.registry_version_id,
        observation_started_at=NOW,
        observation_completed_at=NOW,
        external_snapshot_identity=None,
        external_high_water="asana-event-500",
        corpus_manifest_sha256=_independent_reconciliation_corpus_sha256(
            candidate=candidate, membership=membership
        ),
        scope_complete=True,
        adapter_contract_version="asana-high-water-v1",
        evidence_recorded_at=NOW,
        status="complete",
        expected_items=len(membership),
        processed_items=len(membership),
        started_at=NOW,
        completed_at=NOW,
    )
    session.add(reconciliation)
    session.flush()
    for ordinal, (entity_kind, mapping_id) in enumerate(
        sorted(membership, key=lambda item: (item[0], str(item[1])))
    ):
        session.add(tx.ProjectionReconciliationItem(
            reconciliation_item_id=_next(ids),
            reconciliation_run_id=reconciliation.reconciliation_run_id,
            item_identity=f"{ordinal}:{entity_kind}:{mapping_id}",
            entity_kind=entity_kind,
            mapping_id=mapping_id,
            outcome="matched",
            evidence={"reread": "exact", "boundary": "asana-event-500"},
            recorded_at=NOW,
        ))
    artifact_root = Path(tempfile.mkdtemp(prefix="dish-release-evidence-"))

    def artifact(label: str) -> tuple[str, str]:
        path = artifact_root / f"{label}.json"
        path.write_bytes((label + "\n").encode("utf-8"))
        return str(path), hashlib.sha256(path.read_bytes()).hexdigest()
    for category, key in evidence_contracts:
        service.record_evidence(
            candidate_id=candidate_id,
            category=category,
            evidence_key=key,
            outcome="pass",
            payload={
                "artifact_kind": EXPECTED_EVIDENCE_ARTIFACT_KINDS[(category, key)],
                "artifact_identity": f"fixture:{category}:{key}",
                "artifact_path": artifact(f"evidence-{category}-{key}")[0],
                "artifact_sha256": artifact(f"evidence-{category}-{key}")[1],
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
                    "artifact_path": artifact(f"checkpoint-{kind}-{checkpoint_kind}")[0],
                    "artifact_sha256": artifact(f"checkpoint-{kind}-{checkpoint_kind}")[1],
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


def _record_and_engage_writer_fence(service, ids, *, fence_id, engaged_at):
    observation = service.record_writer_fence_artifact_observation(
        fence_id=fence_id,
        artifact_generation_identity="cutover-fixture-generation-v1",
        canonical_path=f"/tmp/writer-fence-{fence_id}.sqlite3",
        content_sha256="b" * 64,
        filesystem_device=1,
        filesystem_inode=(fence_id.int % 2_000_000_000) + 1,
        verification_result="matched",
        observation_contract_version="writer-fence-fixture-v1",
        observed_at=engaged_at,
        recorded_at=engaged_at,
    )
    return service.engage_writer_fence(
        fence_id=fence_id,
        artifact_observation_id=observation.observation_id,
        engaged_at=engaged_at,
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
    candidate_id=None,
    generation_id=None,
    corpus_identity: str,
    started_at,
    completed_at,
):
    from dish_pg import stage6_models as rel

    if candidate_id is None:
        candidate = session.scalar(
            select(rel.ReleaseCandidate)
            .where(rel.ReleaseCandidate.generation_id == generation_id)
            .order_by(rel.ReleaseCandidate.created_at.desc())
            .limit(1)
        )
    else:
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
    if candidate is None:
        raise AssertionError("reconciliation fixture requires an existing release candidate")
    active_registry = session.get(models.ActiveSectionRegistry, candidate.generation_id)
    membership = _independent_active_mapping_membership(session, candidate=candidate)
    run = tx.ProjectionReconciliationRun(
        reconciliation_run_id=_next(ids),
        generation_id=candidate.generation_id,
        projection_epoch_id=candidate.projection_epoch_id,
        corpus_identity=corpus_identity,
        candidate_id=candidate.candidate_id,
        registry_version_id=active_registry.registry_version_id,
        observation_started_at=started_at,
        observation_completed_at=completed_at,
        external_snapshot_identity=None,
        external_high_water=f"high-water:{corpus_identity}",
        corpus_manifest_sha256=_independent_reconciliation_corpus_sha256(
            candidate=candidate, membership=membership
        ),
        scope_complete=True,
        adapter_contract_version="asana-high-water-v1",
        evidence_recorded_at=completed_at,
        status="complete",
        expected_items=len(membership),
        processed_items=len(membership),
        started_at=started_at,
        completed_at=completed_at,
    )
    session.add(run)
    session.flush()
    for ordinal, (entity_kind, mapping_id) in enumerate(
        sorted(membership, key=lambda item: (item[0], str(item[1])))
    ):
        session.add(tx.ProjectionReconciliationItem(
            reconciliation_item_id=_next(ids),
            reconciliation_run_id=run.reconciliation_run_id,
            item_identity=f"{ordinal}:{entity_kind}:{mapping_id}",
            entity_kind=entity_kind,
            mapping_id=mapping_id,
            outcome="matched",
            evidence={"source": corpus_identity},
            recorded_at=completed_at,
        ))
    session.flush()
    return run


def _artifact_file(label: str) -> tuple[str, str]:
    root = Path(tempfile.mkdtemp(prefix="dish-runtime-artifact-"))
    path = root / f"{label}.bin"
    path.write_bytes((label + "\n").encode("utf-8"))
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_worker_probe_inventory(session, ids, *, candidate, sealed_at):
    inventory = typed_readiness.WorkerProbeInventory(
        inventory_id=_next(ids),
        candidate_id=candidate.candidate_id,
        projection_epoch_id=candidate.projection_epoch_id,
        inventory_version=1,
        required_probe_count=3,
        inventory_sha256="0" * 64,
        inventory_contract_version="worker-probes-v1",
        sealed_at=sealed_at,
    )
    session.add(inventory)
    session.flush()
    requirements = []
    for ordinal, probe_kind in enumerate(("claim", "write", "restart")):
        requirement = typed_readiness.WorkerProbeRequirement(
            requirement_id=_next(ids),
            inventory_id=inventory.inventory_id,
            probe_kind=probe_kind,
            ordinal=ordinal,
            probe_contract_version="projection-worker-probe-v1",
        )
        session.add(requirement)
        requirements.append(requirement)
    session.flush()
    inventory.inventory_sha256 = _independent_worker_inventory_sha256(
        inventory=inventory, requirements=requirements
    )
    session.flush()
    return inventory, requirements


def _complete_worker_readiness(
    session,
    ids,
    *,
    candidate,
    readiness,
    inventory,
    requirements,
    deployed_artifact_sha256,
    completed_at,
):
    for requirement in requirements:
        session.add(typed_readiness.WorkerProbeEvidence(
            evidence_id=_next(ids),
            readiness_id=readiness.readiness_id,
            requirement_id=requirement.requirement_id,
            inventory_id=inventory.inventory_id,
            candidate_id=candidate.candidate_id,
            projection_epoch_id=candidate.projection_epoch_id,
            probe_kind=requirement.probe_kind,
            execution_identity=f"probe:{requirement.probe_kind}:{readiness.readiness_id}",
            worker_identity=readiness.worker_identity,
            deployed_artifact_sha256=deployed_artifact_sha256,
            result="pass",
            observed_at=completed_at,
            evidence_artifact_identity=f"fixture:{requirement.probe_kind}",
            evidence_sha256=_independent_worker_probe_evidence_sha256(
                readiness=readiness,
                requirement=requirement,
                inventory=inventory,
                deployed_artifact_sha256=deployed_artifact_sha256,
                observed_at=completed_at,
            ),
            recorded_at=completed_at,
        ))
    session.flush()
    completion = typed_readiness.WorkerReadinessCompletion(
        completion_id=_next(ids),
        readiness_id=readiness.readiness_id,
        inventory_id=inventory.inventory_id,
        candidate_id=candidate.candidate_id,
        projection_epoch_id=candidate.projection_epoch_id,
        completion_state="complete",
        required_probe_count=inventory.required_probe_count,
        passed_probe_count=inventory.required_probe_count,
        completion_sha256=_independent_worker_completion_sha256(
            readiness=readiness, inventory=inventory, completed_at=completed_at
        ),
        completed_at=completed_at,
    )
    session.add(completion)
    session.flush()
    return completion


def _record_runtime_and_typed_readiness(
    session,
    ids,
    *,
    service,
    candidate_id,
    reconciliation,
    recorded_at,
    worker_identity="projection-worker@fixture",
):
    candidate = service.candidate_status(candidate_id)
    service_path, service_sha = _artifact_file("service-artifact")
    worker_path, worker_sha = _artifact_file("projection-worker-artifact")
    route_path, route_sha = _artifact_file("route-probe")
    runtime = service.record_runtime_release_attestation(
        candidate_id=candidate_id,
        service_artifact_sha256=service_sha,
        projection_worker_artifact_sha256=worker_sha,
        route_probe_sha256=route_sha,
        payload={
            "dish_release": candidate.dish_release,
            "protocol_release": candidate.protocol_release,
            "openapi_release": candidate.openapi_release,
            "routing_release": candidate.routing_release,
            "route_target": "postgresql",
            "health": "pass",
            "mutation_admission": "closed",
            "service_artifact_path": service_path,
            "projection_worker_artifact_path": worker_path,
            "route_probe_path": route_path,
        },
        recorded_at=recorded_at,
    )
    inventory, requirements = _seed_worker_probe_inventory(
        session, ids, candidate=candidate, sealed_at=recorded_at
    )
    readiness_row = service.record_projection_worker_readiness(
        candidate_id=candidate_id,
        reconciliation_run_id=reconciliation.reconciliation_run_id,
        worker_identity=worker_identity,
        worker_release=candidate.dish_release,
        payload={"probe_runner": "typed-worker-probes-v1"},
        ready_at=recorded_at,
    )
    _complete_worker_readiness(
        session,
        ids,
        candidate=candidate,
        readiness=readiness_row,
        inventory=inventory,
        requirements=requirements,
        deployed_artifact_sha256=worker_sha,
        completed_at=recorded_at,
    )
    return runtime, readiness_row
