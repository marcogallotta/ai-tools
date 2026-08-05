"""Native PostgreSQL shadow-baseline admission and termination races."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.transition import ShadowService, TransitionAuthorityError
from tests.support.postgresql.concurrency import (
    TransactionGate,
    assert_transaction_aborted,
    assert_transaction_blocked,
    assert_transaction_committed,
    execute_transaction,
    independent_connections,
)
from tests.support.postgresql.core import core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _next

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


@pytest.fixture(autouse=True)
def _require_native_postgresql(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--postgresql"):
        pytest.skip("native PostgreSQL concurrency certification requires --postgresql")


class _BaselineGateService(ShadowService):
    def __init__(self, *args, gate: TransactionGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate

    def _lock_baseline(self, baseline_id):
        baseline = super()._lock_baseline(baseline_id)
        self._gate.block()
        return baseline


def _seed_baseline(factory, ids, context) -> uuid.UUID:
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="native-shadow-race",
            created_at=NOW,
        )
        return baseline.shadow_baseline_id


def _seed_pending_delivery(factory, ids, context) -> uuid.UUID:
    baseline_id = _seed_baseline(factory, ids, context)
    with session_scope(factory) as session:
        _capture(ShadowService(session, uuid_factory=lambda: _next(ids)), baseline_id, "pending")
    return baseline_id


def _capture(service: ShadowService, baseline_id: uuid.UUID, identity: str):
    return service.capture_envelope(
        shadow_baseline_id=baseline_id,
        command_name="prepare",
        source_request_identity=identity,
        canonical_input={"command": "prepare", "arguments": {}},
        source_outcome={"ok": True},
        source_post_state={},
        rollout_sequence=1,
        source_authority_generation="legacy-1",
        captured_at=NOW,
    )


def test_native_admitted_capture_blocks_close_and_forces_close_to_reread(core_db) -> None:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    baseline_id = _seed_baseline(factory, ids, context)
    gate = TransactionGate(label="shadow capture admitted under baseline lock")
    engine = factory.kw["bind"]

    with independent_connections(engine) as (capture_connection, close_connection):
        def capture(session):
            return _capture(
                _BaselineGateService(session, uuid_factory=uuid.uuid4, gate=gate),
                baseline_id,
                "admitted-capture",
            )

        def close(session):
            return ShadowService(session).close_baseline(
                baseline_id=baseline_id,
                closed_at=NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            capture_future = pool.submit(execute_transaction, capture_connection, capture)
            gate.wait_until_blocked()
            close_future = pool.submit(execute_transaction, close_connection, close)
            assert_transaction_blocked(close_future)
            gate.release()
            assert_transaction_committed(capture_future.result(timeout=30))
            close_outcome = close_future.result(timeout=30)

    assert_transaction_aborted(close_outcome, error_type=TransitionAuthorityError)
    with session_scope(factory) as session:
        baseline = session.get(tx.ShadowBaseline, baseline_id)
        assert baseline.status == "open"
        assert session.scalar(
            select(func.count()).select_from(tx.ShadowEnvelope).where(
                tx.ShadowEnvelope.shadow_baseline_id == baseline_id
            )
        ) == 1


def test_native_committed_close_rejects_waiting_capture(core_db) -> None:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    baseline_id = _seed_baseline(factory, ids, context)
    gate = TransactionGate(label="shadow baseline close owns termination lock")
    engine = factory.kw["bind"]

    with independent_connections(engine) as (close_connection, capture_connection):
        def close(session):
            return _BaselineGateService(session, gate=gate).close_baseline(
                baseline_id=baseline_id,
                closed_at=NOW,
            )

        def capture(session):
            return _capture(ShadowService(session), baseline_id, "after-close")

        with ThreadPoolExecutor(max_workers=2) as pool:
            close_future = pool.submit(execute_transaction, close_connection, close)
            gate.wait_until_blocked()
            capture_future = pool.submit(execute_transaction, capture_connection, capture)
            assert_transaction_blocked(capture_future)
            gate.release()
            assert_transaction_committed(close_future.result(timeout=30))
            capture_outcome = capture_future.result(timeout=30)

    assert_transaction_aborted(capture_outcome, error_type=TransitionAuthorityError)
    with session_scope(factory) as session:
        baseline = session.get(tx.ShadowBaseline, baseline_id)
        assert baseline.status == "closed"
        assert session.scalar(
            select(func.count()).select_from(tx.ShadowEnvelope).where(
                tx.ShadowEnvelope.shadow_baseline_id == baseline_id
            )
        ) == 0


def test_native_committed_disqualification_rejects_waiting_capture(core_db) -> None:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    baseline_id = _seed_baseline(factory, ids, context)
    gate = TransactionGate(label="shadow baseline disqualification owns termination lock")
    engine = factory.kw["bind"]

    with independent_connections(engine) as (termination_connection, capture_connection):
        def disqualify(session):
            return _BaselineGateService(session, gate=gate).disqualify_baseline(
                baseline_id=baseline_id,
                reason="native race disqualification",
                at=NOW,
            )

        def capture(session):
            return _capture(ShadowService(session), baseline_id, "after-disqualification")

        with ThreadPoolExecutor(max_workers=2) as pool:
            termination_future = pool.submit(
                execute_transaction, termination_connection, disqualify
            )
            gate.wait_until_blocked()
            capture_future = pool.submit(execute_transaction, capture_connection, capture)
            assert_transaction_blocked(capture_future)
            gate.release()
            assert_transaction_committed(termination_future.result(timeout=30))
            capture_outcome = capture_future.result(timeout=30)

    assert_transaction_aborted(capture_outcome, error_type=TransitionAuthorityError)
    with session_scope(factory) as session:
        baseline = session.get(tx.ShadowBaseline, baseline_id)
        assert baseline.status == "disqualified"
        assert baseline.disqualification_reason == "native race disqualification"
        assert session.scalar(
            select(func.count()).select_from(tx.ShadowEnvelope).where(
                tx.ShadowEnvelope.shadow_baseline_id == baseline_id
            )
        ) == 0


def test_native_committed_disqualification_rejects_waiting_delivery_claim(core_db) -> None:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    baseline_id = _seed_pending_delivery(factory, ids, context)
    gate = TransactionGate(label="shadow disqualification fences recovery claim")
    engine = factory.kw["bind"]

    with independent_connections(engine) as (termination_connection, claim_connection):
        def disqualify(session):
            return _BaselineGateService(session, gate=gate).disqualify_baseline(
                baseline_id=baseline_id,
                reason="native recovery authority changed",
                at=NOW,
            )

        def claim(session):
            return ShadowService(session).claim_delivery(
                worker_id="restarted-worker",
                claim_token=uuid.uuid4(),
                now=NOW + timedelta(minutes=5),
                ttl=timedelta(minutes=2),
                shadow_baseline_id=baseline_id,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            termination_future = pool.submit(
                execute_transaction, termination_connection, disqualify
            )
            gate.wait_until_blocked()
            claim_future = pool.submit(execute_transaction, claim_connection, claim)
            assert_transaction_blocked(claim_future)
            gate.release()
            assert_transaction_committed(termination_future.result(timeout=30))
            claim_outcome = claim_future.result(timeout=30)

    assert assert_transaction_committed(claim_outcome) is None
    with session_scope(factory) as session:
        delivery = session.scalar(
            select(tx.ShadowDelivery)
            .join(tx.ShadowEnvelope, tx.ShadowEnvelope.envelope_id == tx.ShadowDelivery.envelope_id)
            .where(tx.ShadowEnvelope.shadow_baseline_id == baseline_id)
        )
        assert delivery.state == "pending"
        assert delivery.attempts == 0
