"""Focused authority and recovery contracts for shadow delivery."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import ShadowWorker
from dish_pg.transition import ShadowService, TransitionAuthorityError
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def _authority_setup(session, ids, context):
    service = ShadowService(session, uuid_factory=lambda: _next(ids))
    baseline = service.create_baseline(
        generation_id=context["generation_id"],
        source_generation_identity="legacy-1",
        source_commit="worktree",
        created_at=NOW,
    )
    return service, baseline


def _authority_envelope(service, baseline_id, *, identity, sequence, captured_at=NOW):
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


def test_delivery_settlement_requires_current_owner_revision_and_unexpired_claim(workflow_db):
    factory, ids, context, _task = workflow_db
    token = uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        envelope = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="settlement-authority",
            sequence=1,
        )
        claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=token,
            now=NOW,
            ttl=timedelta(minutes=1),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert claim is not None
        with pytest.raises(TransitionAuthorityError, match="stale or expired"):
            service.compare_delivery(
                delivery_id=claim.delivery_id,
                claim_token=token,
                claim_revision=claim.delivery_revision,
                worker_id="worker-2",
                target_result=dict(envelope.source_outcome),
                comparator_release="test",
                compared_at=NOW + timedelta(seconds=1),
            )
        with pytest.raises(TransitionAuthorityError, match="stale or expired"):
            service.compare_delivery(
                delivery_id=claim.delivery_id,
                claim_token=token,
                claim_revision=claim.delivery_revision + 1,
                worker_id="worker-1",
                target_result=dict(envelope.source_outcome),
                comparator_release="test",
                compared_at=NOW + timedelta(seconds=1),
            )
        with pytest.raises(TransitionAuthorityError, match="stale or expired"):
            service.compare_delivery(
                delivery_id=claim.delivery_id,
                claim_token=token,
                claim_revision=claim.delivery_revision,
                worker_id="worker-1",
                target_result=dict(envelope.source_outcome),
                comparator_release="test",
                compared_at=NOW + timedelta(minutes=1),
            )
        current = session.get(tx.ShadowDelivery, claim.delivery_id)
        assert current.state == "claimed"
        assert current.delivery_revision == claim.delivery_revision


def test_superseded_delivery_worker_cannot_settle_new_claim(workflow_db):
    factory, ids, context, _task = workflow_db
    stale_token, current_token = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        envelope = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="superseded-settlement",
            sequence=1,
        )
        stale_claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=stale_token,
            now=NOW,
            ttl=timedelta(minutes=1),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        current_claim = service.claim_delivery(
            worker_id="worker-2",
            claim_token=current_token,
            now=NOW + timedelta(minutes=2),
            ttl=timedelta(minutes=1),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert stale_claim is not None and current_claim is not None
        with pytest.raises(TransitionAuthorityError, match="stale or expired"):
            service.compare_delivery(
                delivery_id=stale_claim.delivery_id,
                claim_token=stale_token,
                claim_revision=stale_claim.delivery_revision,
                worker_id="worker-1",
                target_result=dict(envelope.source_outcome),
                comparator_release="test",
                compared_at=NOW + timedelta(minutes=2, seconds=1),
            )
        comparison = service.compare_delivery(
            delivery_id=current_claim.delivery_id,
            claim_token=current_token,
            claim_revision=current_claim.delivery_revision,
            worker_id="worker-2",
            target_result=dict(envelope.source_outcome),
            comparator_release="test",
            compared_at=NOW + timedelta(minutes=2, seconds=1),
        )
        assert comparison.parity_class == "exact"


def test_capture_and_gap_admission_stop_after_baseline_termination(workflow_db):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        service.close_baseline(baseline_id=baseline.shadow_baseline_id, closed_at=NOW)
        with pytest.raises(TransitionAuthorityError, match="baseline is not open"):
            _authority_envelope(
                service,
                baseline.shadow_baseline_id,
                identity="capture-after-close",
                sequence=1,
            )
        with pytest.raises(TransitionAuthorityError, match="baseline is not open"):
            service.record_gap(
                baseline_id=baseline.shadow_baseline_id,
                gap_identity="capture:after-close",
                gap_kind="missing_envelope",
                details={"failure_stage": "completion"},
                created_at=NOW,
            )


def test_delivery_failure_recovery_requires_exact_not_applied_proof(workflow_db):
    factory, ids, context, _task = workflow_db
    token = uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        envelope = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="manual-recovery-proof",
            sequence=1,
        )
        claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=token,
            now=NOW,
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert claim is not None
        gap = service.fail_delivery(
            delivery_id=claim.delivery_id,
            claim_token=token,
            claim_revision=claim.delivery_revision,
            worker_id="worker-1",
            error="commit result could not be proved",
            failed_at=NOW + timedelta(seconds=1),
        )
        failed_revision = gap.details["failed_delivery_revision"]

        with pytest.raises(TransitionAuthorityError, match="remains uncertain"):
            service.resolve_gap(
                gap_id=gap.gap_id,
                resolution={"delivery_outcome": "uncertain", "evidence": "timeout"},
                resolved_at=NOW + timedelta(seconds=2),
            )
        session.expire_all()
        current_gap = session.get(tx.ShadowGap, gap.gap_id)
        current_delivery = session.scalar(
            select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == envelope.envelope_id)
        )
        assert current_gap.state == "open"
        assert current_delivery.state == "failed"
        assert current_delivery.delivery_revision == failed_revision

        resolved = service.resolve_gap(
            gap_id=gap.gap_id,
            resolution={
                "delivery_outcome": "not_applied",
                "evidence": "exact request journal proves rollback",
            },
            resolved_at=NOW + timedelta(seconds=3),
        )
        session.expire_all()
        current_delivery = session.scalar(
            select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == envelope.envelope_id)
        )
        assert resolved.state == "resolved"
        assert current_delivery.state == "pending"
        assert current_delivery.delivery_revision == failed_revision + 1


def test_failed_delivery_cannot_requeue_after_later_rollout_evaluated(workflow_db):
    factory, ids, context, _task = workflow_db
    first_token, second_token = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        first = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="failed-before-later-progress",
            sequence=1,
        )
        second = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="later-progress",
            sequence=2,
            captured_at=NOW + timedelta(seconds=1),
        )
        first_claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=first_token,
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert first_claim is not None
        gap = service.fail_delivery(
            delivery_id=first_claim.delivery_id,
            claim_token=first_token,
            claim_revision=first_claim.delivery_revision,
            worker_id="worker-1",
            error="definite rollback",
            failed_at=NOW + timedelta(seconds=3),
        )
        later_claim = service.claim_delivery(
            worker_id="worker-2",
            claim_token=second_token,
            now=NOW + timedelta(seconds=4),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert later_claim is not None
        assert later_claim.envelope_id == second.envelope_id
        service.compare_delivery(
            delivery_id=later_claim.delivery_id,
            claim_token=second_token,
            claim_revision=later_claim.delivery_revision,
            worker_id="worker-2",
            target_result=dict(second.source_outcome),
            comparator_release="test",
            compared_at=NOW + timedelta(seconds=5),
        )

        with pytest.raises(
            TransitionAuthorityError,
            match="later rollout is in flight or after later rollout evaluation",
        ):
            service.resolve_gap(
                gap_id=gap.gap_id,
                resolution={
                    "delivery_outcome": "not_applied",
                    "evidence": "exact request journal proves rollback",
                },
                resolved_at=NOW + timedelta(seconds=6),
            )
        session.expire_all()
        current_gap = session.get(tx.ShadowGap, gap.gap_id)
        current_delivery = session.scalar(
            select(tx.ShadowDelivery).where(
                tx.ShadowDelivery.envelope_id == first.envelope_id
            )
        )
        assert current_gap.state == "open"
        assert current_delivery.state == "failed"


@pytest.mark.parametrize("later_outcome", ["failed", "skipped", "voided"])
def test_failed_delivery_can_requeue_after_later_terminal_no_evaluation(
    workflow_db, later_outcome
):
    factory, ids, context, _task = workflow_db
    first_token, second_token = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        first = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity=f"failed-before-later-{later_outcome}",
            sequence=1,
        )
        second = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity=f"later-{later_outcome}",
            sequence=2,
            captured_at=NOW + timedelta(seconds=1),
        )
        first_claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=first_token,
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert first_claim is not None
        gap = service.fail_delivery(
            delivery_id=first_claim.delivery_id,
            claim_token=first_token,
            claim_revision=first_claim.delivery_revision,
            worker_id="worker-1",
            error="definite rollback",
            failed_at=NOW + timedelta(seconds=3),
        )
        later_claim = service.claim_delivery(
            worker_id="worker-2",
            claim_token=second_token,
            now=NOW + timedelta(seconds=4),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert later_claim is not None
        assert later_claim.envelope_id == second.envelope_id
        if later_outcome == "failed":
            service.fail_delivery(
                delivery_id=later_claim.delivery_id,
                claim_token=second_token,
                claim_revision=later_claim.delivery_revision,
                worker_id="worker-2",
                error="later evaluation rolled back",
                failed_at=NOW + timedelta(seconds=5),
            )
        elif later_outcome == "skipped":
            service.skip_delivery(
                delivery_id=later_claim.delivery_id,
                claim_token=second_token,
                claim_revision=later_claim.delivery_revision,
                worker_id="worker-2",
                reason="capture-only evidence",
                comparator_release="test",
                completed_at=NOW + timedelta(seconds=5),
            )
        else:
            service.fail_delivery(
                delivery_id=later_claim.delivery_id,
                claim_token=second_token,
                claim_revision=later_claim.delivery_revision,
                worker_id="worker-2",
                error="later evaluation rolled back",
                failed_at=NOW + timedelta(seconds=5),
            )
            later_delivery = session.scalar(
                select(tx.ShadowDelivery).where(
                    tx.ShadowDelivery.envelope_id == second.envelope_id
                )
            )
            assert later_delivery is not None
            service.void_failed_delivery(
                delivery_id=later_delivery.delivery_id,
                reason="permanently abandon later evaluation",
                comparator_release="test",
                completed_at=NOW + timedelta(seconds=6),
            )

        resolved = service.resolve_gap(
            gap_id=gap.gap_id,
            resolution={
                "delivery_outcome": "not_applied",
                "evidence": "exact request journal proves rollback",
            },
            resolved_at=NOW + timedelta(seconds=7),
        )
        session.expire_all()
        first_delivery = session.scalar(
            select(tx.ShadowDelivery).where(
                tx.ShadowDelivery.envelope_id == first.envelope_id
            )
        )
        assert resolved.state == "resolved"
        assert first_delivery.state == "pending"


def test_failed_delivery_cannot_requeue_while_later_rollout_is_claimed(workflow_db):
    factory, ids, context, _task = workflow_db
    first_token, second_token = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="failed-before-later-in-flight",
            sequence=1,
        )
        second = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="later-in-flight",
            sequence=2,
            captured_at=NOW + timedelta(seconds=1),
        )
        first_claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=first_token,
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert first_claim is not None
        gap = service.fail_delivery(
            delivery_id=first_claim.delivery_id,
            claim_token=first_token,
            claim_revision=first_claim.delivery_revision,
            worker_id="worker-1",
            error="definite rollback",
            failed_at=NOW + timedelta(seconds=3),
        )
        later_claim = service.claim_delivery(
            worker_id="worker-2",
            claim_token=second_token,
            now=NOW + timedelta(seconds=4),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert later_claim is not None
        assert later_claim.envelope_id == second.envelope_id

        with pytest.raises(
            TransitionAuthorityError,
            match="later rollout is in flight or after later rollout evaluation",
        ):
            service.resolve_gap(
                gap_id=gap.gap_id,
                resolution={
                    "delivery_outcome": "not_applied",
                    "evidence": "exact request journal proves rollback",
                },
                resolved_at=NOW + timedelta(seconds=5),
            )


def test_stale_manual_recovery_cannot_reopen_disqualified_delivery(workflow_db):
    factory, ids, context, _task = workflow_db
    token = uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        envelope = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="stale-manual-recovery",
            sequence=1,
        )
        claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=token,
            now=NOW,
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert claim is not None
        gap = service.fail_delivery(
            delivery_id=claim.delivery_id,
            claim_token=token,
            claim_revision=claim.delivery_revision,
            worker_id="worker-1",
            error="definite rollback",
            failed_at=NOW + timedelta(seconds=1),
        )
        failed_revision = gap.details["failed_delivery_revision"]
        service.disqualify_baseline(
            baseline_id=baseline.shadow_baseline_id,
            reason="target authority superseded",
            at=NOW + timedelta(seconds=2),
        )

        with pytest.raises(TransitionAuthorityError, match="no longer current"):
            service.resolve_gap(
                gap_id=gap.gap_id,
                resolution={"delivery_outcome": "not_applied", "evidence": "late proof"},
                resolved_at=NOW + timedelta(seconds=3),
            )
        session.expire_all()
        current_gap = session.get(tx.ShadowGap, gap.gap_id)
        current_delivery = session.scalar(
            select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == envelope.envelope_id)
        )
        assert current_gap.state == "open"
        assert current_delivery.state == "failed"
        assert current_delivery.delivery_revision == failed_revision


def test_prior_failure_gap_cannot_recover_a_later_failed_revision(workflow_db):
    factory, ids, context, _task = workflow_db
    first_token, second_token = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service, baseline = _authority_setup(session, ids, context)
        envelope = _authority_envelope(
            service,
            baseline.shadow_baseline_id,
            identity="repeated-delivery-failure",
            sequence=1,
        )
        first_claim = service.claim_delivery(
            worker_id="worker-1",
            claim_token=first_token,
            now=NOW,
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert first_claim is not None
        first_gap = service.fail_delivery(
            delivery_id=first_claim.delivery_id,
            claim_token=first_token,
            claim_revision=first_claim.delivery_revision,
            worker_id="worker-1",
            error="first definite rollback",
            failed_at=NOW + timedelta(seconds=1),
        )
        service.resolve_gap(
            gap_id=first_gap.gap_id,
            resolution={"delivery_outcome": "not_applied", "evidence": "first rollback"},
            resolved_at=NOW + timedelta(seconds=2),
        )
        second_claim = service.claim_delivery(
            worker_id="worker-2",
            claim_token=second_token,
            now=NOW + timedelta(seconds=3),
            ttl=timedelta(minutes=2),
            shadow_baseline_id=baseline.shadow_baseline_id,
        )
        assert second_claim is not None
        second_gap = service.fail_delivery(
            delivery_id=second_claim.delivery_id,
            claim_token=second_token,
            claim_revision=second_claim.delivery_revision,
            worker_id="worker-2",
            error="second definite rollback",
            failed_at=NOW + timedelta(seconds=4),
        )
        assert second_gap.gap_id != first_gap.gap_id
        assert (
            second_gap.details["failed_delivery_revision"]
            > first_gap.details["failed_delivery_revision"]
        )

        with pytest.raises(TransitionAuthorityError, match="gap is not open"):
            service.resolve_gap(
                gap_id=first_gap.gap_id,
                resolution={"delivery_outcome": "not_applied", "evidence": "stale replay"},
                resolved_at=NOW + timedelta(seconds=5),
            )
        session.expire_all()
        current_delivery = session.scalar(
            select(tx.ShadowDelivery).where(tx.ShadowDelivery.envelope_id == envelope.envelope_id)
        )
        assert current_delivery.state == "failed"
        assert (
            current_delivery.delivery_revision
            == second_gap.details["failed_delivery_revision"]
        )


class UncertainEvaluator:
    def evaluate(self, session, envelope):
        del session, envelope
        return {
            "ok": False,
            "command": "prepare",
            "code": "BACKEND_UNCERTAIN",
            "data": {"state": "uncertain"},
            "retryable": False,
        }


def test_shadow_worker_preserves_uncertain_target_result_without_retry(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id

    spool = ShadowSpool(tmp_path / "spool.sqlite3")
    reservation = spool.reserve(
        source_request_identity="request-execute",
        source_authority_generation="legacy-1",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {}},
        principal={},
        source_pre_state={"phase": "research"},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW,
    )
    spool.complete(
        reservation.registration_id,
        source_outcome={"ok": True},
        source_post_state={"phase": "research"},
        source_effects={},
        completed_at=NOW,
    )
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=UncertainEvaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=tmp_path / "dark-launch.disabled",
        clock=lambda: NOW,
    )

    assert worker.run_once() is True
    with session_scope(factory) as session:
        comparison = session.scalar(select(tx.ShadowComparison))
        delivery = session.scalar(select(tx.ShadowDelivery))
        assert comparison.target_result["code"] == "BACKEND_UNCERTAIN"
        assert comparison.target_result["data"]["state"] == "uncertain"
        assert comparison.parity_class == "mismatch"
        assert delivery.state == "delivered"
        assert delivery.attempts == 1
