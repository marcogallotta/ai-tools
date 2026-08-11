"""Native Stage 6 activation/checkpoint restart rehearsal."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from dish_pg.database import session_scope
from dish_pg.release import ReleaseCandidateService
from dish_service.legacy_writer_fence import (
    engage_legacy_writer_fence,
    observe_legacy_writer_fence,
)
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db
from tests.support.postgresql.cutover_activation_checkpoint import (
    assert_checkpoint_survives_process_death,
    start_stale_writer_probe,
)
from tests.support.postgresql.first_admission import (
    _record_committed_first_request,
)
from tests.support.postgresql.process_failure import BarrierServer, write_scenario
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.release import (
    HASH_A,
    _complete_active_mapping_reconciliation,
    _prepare_candidate,
    _record_final_closure,
    _record_runtime_and_worker_readiness_report,
    _writer_fence_proof,
)
from tests.support.postgresql.workflow import NOW, _next, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_stage6_activation_checkpoints_survive_process_death_and_stale_writer_is_fenced(
    core_db, tmp_path: Path
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    fence_path = tmp_path / "governed" / "legacy-writer-fence.json"
    fence_path.parent.mkdir()

    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        candidate = service.candidate_status(candidate_id)
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
            closed_through_at=NOW + timedelta(minutes=5),
        )
        service.approve_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle.bundle_id,
            approver="Marco",
            approval_statement="TEST-only exact candidate approval fixture.",
            approval_payload={
                "decision": "approved",
                "final_asana_closure_id": str(closure.closure_id),
                "final_asana_closure_sha256": closure.closure_sha256,
            },
            approved_at=NOW + timedelta(minutes=5),
        )
        fence = service.prepare_writer_fence(
            candidate_id=candidate_id,
            target_identity="legacy-service@rehearsal-host",
            mechanism="fail-closed-file",
            manifest={"path": str(fence_path.resolve())},
            prepared_at=NOW + timedelta(minutes=5),
        )
        cutover = service.prepare_cutover(
            candidate_id=candidate_id,
            started_at=NOW + timedelta(minutes=5),
        )
        cutover_id = cutover.cutover_run_id
        generation_id = candidate.generation_id
        source_release = candidate.source_release
        source_commit = candidate.source_commit

    checkpoint_evidence = [
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="prepared",
        )
    ]

    stale_output = tmp_path / "stale-writer-result.json"
    with BarrierServer() as stale_barrier:
        stale = start_stale_writer_probe(
            fence_path=fence_path,
            tmp_path=tmp_path,
            output=stale_output,
            barrier=stale_barrier,
        )
        ready = stale_barrier.wait("stale_process_ready")
        try:
            manifest, manifest_digest = engage_legacy_writer_fence(
                fence_path,
                fence_id=str(fence.fence_id),
                candidate_id=str(candidate_id),
                source_release=source_release,
                source_commit=source_commit,
                engaged_at=NOW + timedelta(minutes=5),
                operator="TEST-only rehearsal operator",
            )
            observation = observe_legacy_writer_fence(
                fence_path,
                expected_path=fence_path,
                expected_manifest_sha256=fence.manifest_sha256,
                clock=lambda: NOW + timedelta(minutes=5),
            )
            assert manifest_digest == fence.manifest_sha256
            assert observation.manifest_sha256 == fence.manifest_sha256
            assert manifest["path"] == str(fence_path.resolve())
            with session_scope(factory) as session:
                service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
                durable_observation = service.record_writer_fence_artifact_observation(
                    fence_id=fence.fence_id,
                    artifact_generation_identity="stage6-activation-rehearsal-v1",
                    canonical_path=observation.observed_path,
                    content_sha256=observation.artifact_sha256,
                    filesystem_device=int(observation.device),
                    filesystem_inode=int(observation.inode),
                    verification_result="matched",
                    observation_contract_version="writer-fence-observation-v1",
                    observed_at=observation.observed_at,
                    recorded_at=observation.observed_at,
                )
                service.engage_writer_fence(
                    fence_id=fence.fence_id,
                    artifact_observation_id=durable_observation.observation_id,
                    engaged_at=NOW + timedelta(minutes=5),
                )
                persisted = service.writer_fence_status(fence.fence_id)
                inventory = {persisted.target_identity}
                service.verify_writer_fence(
                    fence_id=fence.fence_id,
                    proof=_writer_fence_proof(persisted, candidate_id),
                    verified_at=NOW + timedelta(minutes=5),
                    required_writer_inventory=inventory,
                )
                service.mark_fenced(
                    cutover_run_id=cutover_id,
                    recorded_at=NOW + timedelta(minutes=5),
                    required_writer_inventory=inventory,
                )
                service.recertify_candidate(
                    candidate_id=candidate_id,
                    closure_id=closure.closure_id,
                    approver="Marco",
                    recertification_statement=(
                        "TEST-only final closure remains exact after writer fencing."
                    ),
                    payload={"result": "pass", "scope": "rehearsal"},
                    recertified_at=NOW + timedelta(minutes=5),
                )
            ready.release()
            stale.wait()
        finally:
            if stale.process.poll() is None:
                ready.close()
                stale.kill()

    stale_result = json.loads(stale_output.read_text(encoding="utf-8"))
    assert stale_result["rejected"] is True
    assert stale_result["manifest_sha256"] == fence.manifest_sha256

    checkpoint_evidence.append(
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="fenced",
        )
    )

    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.activate_authority(
            cutover_run_id=cutover_id,
            final_asana_closure_id=closure.closure_id,
            activated_at=NOW + timedelta(minutes=5),
            required_writer_inventory={"legacy-service@rehearsal-host"},
        )
    checkpoint_evidence.append(
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="activated",
        )
    )

    with session_scope(factory) as session:
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.burn_rollback(
            cutover_run_id=cutover_id,
            legacy_bundle_id="legacy-bundle-sha256:" + HASH_A,
            burned_at=NOW + timedelta(minutes=6),
            required_writer_inventory={"legacy-service@rehearsal-host"},
        )
    checkpoint_evidence.append(
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="rollback_burned",
        )
    )

    first_request_id = _next(ids)
    first_run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=generation_id, run_id=first_run_id)
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        reconciliation = _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=generation_id,
            corpus_identity="activation-rehearsal-worker-readiness",
            started_at=NOW + timedelta(minutes=6),
            completed_at=NOW + timedelta(minutes=6),
        )
        _record_runtime_and_worker_readiness_report(
            session,
            ids,
            service=service,
            candidate_id=candidate_id,
            reconciliation=reconciliation,
            recorded_at=NOW + timedelta(minutes=6),
        )
        service.plan_first_admission(
            cutover_run_id=cutover_id,
            request_id=first_request_id,
            command_name="start",
            command_arguments={"task_id": str(task_id), "agent": "codex", "kind": "initial"},
            task_id=task_id,
            owner_id="owner-1",
            principal_class="agent",
            run_id=first_run_id,
            payload={"probe": "stage6 activation rehearsal first mutation"},
            recorded_at=NOW + timedelta(minutes=6),
        )
        control = service.open_mutation_admission(
            cutover_run_id=cutover_id,
            opened_at=NOW + timedelta(minutes=7),
        )
        assert control.state == "closed"
    checkpoint_evidence.append(
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="admission_open",
        )
    )

    _record_committed_first_request(
        factory,
        ids,
        context,
        task_id,
        cutover_id,
        first_request_id,
        first_run_id,
    )
    with session_scope(factory) as session:
        _complete_active_mapping_reconciliation(
            session,
            ids,
            generation_id=generation_id,
            corpus_identity="activation-rehearsal-post-first-admission",
            started_at=NOW + timedelta(minutes=8),
            completed_at=NOW + timedelta(minutes=9),
        )
        service = ReleaseCandidateService(session, uuid_factory=lambda: _next(ids))
        service.verify_first_admission(
            cutover_run_id=cutover_id,
            request_id=first_request_id,
            verified_at=NOW + timedelta(minutes=9),
        )
    checkpoint_evidence.append(
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="first_admission_verified",
        )
    )

    with session_scope(factory) as session:
        ReleaseCandidateService(session, uuid_factory=lambda: _next(ids)).complete_cutover(
            cutover_run_id=cutover_id,
            completed_at=NOW + timedelta(minutes=10),
        )
    checkpoint_evidence.append(
        assert_checkpoint_survives_process_death(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            cutover_run_id=cutover_id,
            generation_id=generation_id,
            expected_state="completed",
        )
    )

    assert [item["state"] for item in checkpoint_evidence] == [
        "prepared",
        "fenced",
        "activated",
        "rollback_burned",
        "admission_open",
        "first_admission_verified",
        "completed",
    ]
    assert checkpoint_evidence[2]["snapshot"]["authority_activation_count"] == 0
    assert checkpoint_evidence[3]["snapshot"]["authority_activation_count"] == 1
    assert checkpoint_evidence[4]["snapshot"]["mutation_admission"]["state"] == "closed"
    assert checkpoint_evidence[5]["snapshot"]["mutation_admission"]["state"] == "open"

    write_scenario(
        "cutover-activation-checkpoints",
        {
            "completion_state": "scenario_assertions_completed",
            "candidate_id": str(candidate_id),
            "generation_id": str(generation_id),
            "cutover_run_id": str(cutover_id),
            "writer_fence": {
                "path": str(fence_path.resolve()),
                "manifest_sha256": fence.manifest_sha256,
                "stale_process_started_before_engagement": True,
                "stale_process_rejected_after_engagement": stale_result["rejected"],
            },
            "checkpoint_process_death": checkpoint_evidence,
        },
        nodeid=(
            "tests/postgresql/native/test_cutover_activation_checkpoint_rehearsal.py::"
            "test_stage6_activation_checkpoints_survive_process_death_and_stale_writer_is_fenced"
        ),
        tmp_path=tmp_path,
    )
