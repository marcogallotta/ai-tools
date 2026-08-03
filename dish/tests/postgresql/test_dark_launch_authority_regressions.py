from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import ShadowWorker
from dish_pg.transition import ShadowService, TransitionAuthorityError
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _baseline(session, ids, context):
    return ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
        generation_id=context["generation_id"],
        source_generation_identity="legacy-1",
        source_commit="worktree",
        created_at=NOW,
    )


def _capture(service, baseline_id, *, identity, sequence, captured_at=NOW):
    return service.capture_envelope(
        shadow_baseline_id=baseline_id,
        command_name="prepare",
        source_request_identity=identity,
        canonical_input={"command": "prepare", "arguments": {}},
        source_outcome={"ok": True, "command": "prepare", "code": "OK"},
        source_post_state={"selected_tables": [], "tables": {}},
        rollout_sequence=sequence,
        source_authority_generation="legacy-1",
        captured_at=captured_at,
    )


def test_envelope_requires_exact_source_and_active_target_generation(workflow_db):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = _baseline(session, ids, context)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        with pytest.raises(TransitionAuthorityError, match="source generation is required"):
            service.capture_envelope(
                shadow_baseline_id=baseline.shadow_baseline_id,
                command_name="prepare",
                source_request_identity="missing-generation",
                canonical_input={"command": "prepare", "arguments": {}},
                source_outcome={"ok": True},
                source_post_state={},
                captured_at=NOW,
            )
        with pytest.raises(TransitionAuthorityError, match="rollout sequence is required"):
            service.capture_envelope(
                shadow_baseline_id=baseline.shadow_baseline_id,
                command_name="prepare",
                source_request_identity="missing-sequence",
                canonical_input={"command": "prepare", "arguments": {}},
                source_outcome={"ok": True},
                source_post_state={},
                source_authority_generation="legacy-1",
                capture_qualification="execute",
                captured_at=NOW,
            )
        with pytest.raises(TransitionAuthorityError, match="source generation"):
            service.capture_envelope(
                shadow_baseline_id=baseline.shadow_baseline_id,
                command_name="prepare",
                source_request_identity="wrong-generation",
                canonical_input={"command": "prepare", "arguments": {}},
                source_outcome={"ok": True},
                source_post_state={},
                source_authority_generation="legacy-2",
                captured_at=NOW,
            )
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        generation.status = "retired"
        generation.retired_at = NOW
        session.flush()
        with pytest.raises(TransitionAuthorityError, match="target generation is stale"):
            service.capture_envelope(
                shadow_baseline_id=baseline.shadow_baseline_id,
                command_name="prepare",
                source_request_identity="stale-target",
                canonical_input={"command": "prepare", "arguments": {}},
                source_outcome={"ok": True},
                source_post_state={},
                source_authority_generation="legacy-1",
                captured_at=NOW,
            )


def test_delivery_claim_is_strictly_rollout_ordered_and_blocks_later_sequence(workflow_db):
    factory, ids, context, _task = workflow_db
    first_token, second_token = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        baseline = _baseline(session, ids, context)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        second = _capture(
            service,
            baseline.shadow_baseline_id,
            identity="sequence-2",
            sequence=2,
            captured_at=NOW,
        )
        first = _capture(
            service,
            baseline.shadow_baseline_id,
            identity="sequence-1",
            sequence=1,
            captured_at=NOW + timedelta(seconds=1),
        )
        claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=first_token,
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert claim is not None
        assert claim.envelope_id == first.envelope_id
        assert service.claim_delivery(
            worker_id="worker-2",
            claim_token=second_token,
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        ) is None
        service.compare_delivery(
            delivery_id=claim.delivery_id,
            claim_token=first_token,
            target_result=dict(first.source_outcome),
            comparator_release="test",
            compared_at=NOW + timedelta(seconds=3),
        )
        next_claim = service.claim_delivery(
            worker_id="worker-2",
            claim_token=second_token,
            now=NOW + timedelta(seconds=4),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert next_claim is not None
        assert next_claim.envelope_id == second.envelope_id


def test_gap_recording_is_exactly_idempotent_and_conflicts_on_changed_evidence(workflow_db):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = _baseline(session, ids, context)
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        first = service.record_gap(
            baseline_id=baseline.shadow_baseline_id,
            gap_identity="capture:request-1",
            gap_kind="missing_envelope",
            details={"failure_stage": "completion"},
            created_at=NOW,
        )
        replay = service.record_gap(
            baseline_id=baseline.shadow_baseline_id,
            gap_identity="capture:request-1",
            gap_kind="missing_envelope",
            details={"failure_stage": "completion"},
            created_at=NOW + timedelta(seconds=1),
        )
        assert replay.gap_id == first.gap_id
        with pytest.raises(TransitionAuthorityError, match="gap identity conflict"):
            service.record_gap(
                baseline_id=baseline.shadow_baseline_id,
                gap_identity="capture:request-1",
                gap_kind="missing_envelope",
                details={"failure_stage": "different"},
                created_at=NOW,
            )


def test_worker_delivery_failure_returns_idle_signal_instead_of_busy_loop(
    workflow_db, tmp_path
):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = _baseline(session, ids, context)
        baseline_id = baseline.shadow_baseline_id
    spool = ShadowSpool(tmp_path / "spool.sqlite3", min_free_bytes=1)
    reservation = spool.reserve(
        source_request_identity="delivery-fails",
        source_authority_generation="legacy-2",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {}},
        principal={},
        source_pre_state={},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW,
    )
    spool.complete(
        reservation.registration_id,
        source_outcome={"ok": True},
        source_post_state={},
        source_effects={},
        completed_at=NOW,
    )
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=object(),
        worker_id="worker-1",
        comparator_release="test",
        kill_switch_path=tmp_path / "disabled",
        clock=lambda: NOW,
    )

    assert worker.run_once() is False
    pending = spool.pending()
    assert len(pending) == 1
    assert pending[0].delivery_attempts == 1
    assert "source generation" in pending[0].last_delivery_error
