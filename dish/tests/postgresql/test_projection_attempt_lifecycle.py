from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.transition import TransitionAuthorityError
from tests.support.postgresql.projection_attempts import (
    external_evidence,
    projection,
    seed_events,
)
from tests.support.postgresql.workflow import NOW, workflow_db


def _stale_settlement_scenario(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    (event_id,) = seed_events(factory, ids, context, task_id)
    with session_scope(factory) as session:
        service = projection(session, ids)
        claim_a = service.claim_next(
            worker_id="worker-a", now=NOW, ttl=timedelta(minutes=1)
        )
        attempt_a = service.begin_attempt(
            event_id=event_id,
            claim_token=claim_a.claim_token,
            claim_revision=claim_a.claim_revision,
            worker_id="worker-a",
            request_identity="same-logical-request",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW,
        )

    reclaimed_at = NOW + timedelta(minutes=2)
    with session_scope(factory) as session:
        claim_b = projection(session, ids).claim_next(
            worker_id="worker-b",
            now=reclaimed_at,
            ttl=timedelta(minutes=1),
        )
        assert claim_b is not None and claim_b.recovery_required

    with pytest.raises(TransitionAuthorityError, match="different claim owner"):
        with session_scope(factory) as session:
            projection(session).record_observation_and_adjudicate(
                attempt_id=attempt_a.attempt_id,
                observation_kind="reread",
                observed_applied=True,
                observed_identity=attempt_a.request_sha256,
                reread_complete=True,
                evidence=external_evidence(
                    observed_identity=attempt_a.request_sha256
                ),
                decided_by="automatic",
                decision_reason="new owner tries to settle predecessor directly",
                observed_at=reclaimed_at,
                claim_token=claim_b.claim_token,
                claim_revision=claim_b.claim_revision,
                worker_id="worker-b",
            )

    with pytest.raises(TransitionAuthorityError, match="stale or expired"):
        with session_scope(factory) as session:
            projection(session).record_observation_and_adjudicate(
                attempt_id=attempt_a.attempt_id,
                observation_kind="reread",
                observed_applied=True,
                observed_identity=attempt_a.request_sha256,
                reread_complete=True,
                evidence=external_evidence(
                    observed_identity=attempt_a.request_sha256
                ),
                decided_by="automatic",
                decision_reason="late stale settlement",
                observed_at=reclaimed_at,
                claim_token=claim_a.claim_token,
                claim_revision=claim_a.claim_revision,
                worker_id="worker-a",
            )

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionObservation).where(
                tx.ProjectionObservation.attempt_id == attempt_a.attempt_id
            )
        ) == 0
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        assert event.claim_owner == "worker-b"
        assert event.claim_token == claim_b.claim_token
        service = projection(session)
        recovery = service.begin_recovery_attempt(
            event_id=event_id,
            claim_token=claim_b.claim_token,
            claim_revision=claim_b.claim_revision,
            worker_id="worker-b",
            prior_attempt_id=claim_b.recovery_attempt.attempt_id,
            started_at=reclaimed_at,
        )
        adjudication = service.record_observation_and_adjudicate(
            attempt_id=recovery.attempt_id,
            observation_kind="reread",
            observed_applied=True,
            observed_identity=recovery.request_sha256,
            reread_complete=True,
            evidence=external_evidence(observed_identity=recovery.request_sha256),
            decided_by="automatic",
            decision_reason="current owner exact reread",
            observed_at=reclaimed_at,
            claim_token=claim_b.claim_token,
            claim_revision=claim_b.claim_revision,
            worker_id="worker-b",
        )
        assert adjudication.outcome == "confirmed"

    with pytest.raises(TransitionAuthorityError, match="not active"):
        with session_scope(factory) as session:
            projection(session).record_observation_and_adjudicate(
                attempt_id=attempt_a.attempt_id,
                observation_kind="reread",
                observed_applied=False,
                observed_identity=None,
                reread_complete=True,
                evidence=external_evidence(absent=True),
                decided_by="automatic",
                decision_reason="duplicate stale settlement",
                observed_at=reclaimed_at,
                claim_token=claim_a.claim_token,
                claim_revision=claim_a.claim_revision,
                worker_id="worker-a",
            )
    with session_scope(factory) as session:
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "applied"
        assert session.get(tx.ProjectionAttempt, recovery.attempt_id).state == "confirmed"


def test_stale_settlement_is_rejected_after_claim_ownership_transfer(
    workflow_db,
) -> None:
    _stale_settlement_scenario(workflow_db)


def _retry_after_not_applied_scenario(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    (event_id,) = seed_events(factory, ids, context, task_id)
    with session_scope(factory) as session:
        service = projection(session, ids)
        first_claim = service.claim_next(
            worker_id="worker-a", now=NOW, ttl=timedelta(minutes=1)
        )
        first_attempt = service.begin_attempt(
            event_id=event_id,
            claim_token=first_claim.claim_token,
            claim_revision=first_claim.claim_revision,
            worker_id="worker-a",
            request_identity="same-logical-request",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW,
        )
        first_result = service.record_observation_and_adjudicate(
            attempt_id=first_attempt.attempt_id,
            observation_kind="reread",
            observed_applied=False,
            observed_identity=None,
            reread_complete=True,
            evidence=external_evidence(absent=True),
            decided_by="automatic",
            decision_reason="external reread proves absence",
            observed_at=NOW,
            claim_token=first_claim.claim_token,
            claim_revision=first_claim.claim_revision,
            worker_id="worker-a",
        )
        assert first_result.outcome == "not_applied"
        immutable = (
            first_attempt.attempt_id,
            first_attempt.dispatch_identity,
            first_attempt.request_sha256,
            first_attempt.state,
            first_attempt.terminal_at,
        )

        second_claim = service.claim_next(
            worker_id="worker-b",
            now=NOW + timedelta(seconds=1),
            ttl=timedelta(minutes=1),
        )
        second_attempt = service.begin_attempt(
            event_id=event_id,
            claim_token=second_claim.claim_token,
            claim_revision=second_claim.claim_revision,
            worker_id="worker-b",
            request_identity="same-logical-request",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW + timedelta(seconds=1),
        )
        assert second_attempt.attempt_id != first_attempt.attempt_id
        assert second_attempt.dispatch_identity != first_attempt.dispatch_identity
        assert second_attempt.attempt_number == 2
        assert second_attempt.retry_generation == 2
        assert second_attempt.state == "dispatched"
        assert (
            first_attempt.attempt_id,
            first_attempt.dispatch_identity,
            first_attempt.request_sha256,
            first_attempt.state,
            first_attempt.terminal_at,
        ) == immutable
        second_result = service.record_observation_and_adjudicate(
            attempt_id=second_attempt.attempt_id,
            observation_kind="reread",
            observed_applied=True,
            observed_identity=second_attempt.request_sha256,
            reread_complete=True,
            evidence=external_evidence(
                observed_identity=second_attempt.request_sha256
            ),
            decided_by="automatic",
            decision_reason="retry externally observed",
            observed_at=NOW + timedelta(seconds=1),
            claim_token=second_claim.claim_token,
            claim_revision=second_claim.claim_revision,
            worker_id="worker-b",
        )
        assert second_result.outcome == "confirmed"
        assert service.claim_next(
            worker_id="worker-c",
            now=NOW + timedelta(seconds=2),
            ttl=timedelta(minutes=1),
        ) is None
        attempts = session.scalars(
            select(tx.ProjectionAttempt)
            .where(tx.ProjectionAttempt.projection_event_id == event_id)
            .order_by(tx.ProjectionAttempt.attempt_number)
        ).all()
        assert [(row.state, row.retry_generation) for row in attempts] == [
            ("not_applied", 1),
            ("confirmed", 2),
        ]


def test_retry_after_not_applied_gets_fresh_durable_attempt_identity(
    workflow_db,
) -> None:
    _retry_after_not_applied_scenario(workflow_db)


@pytest.mark.parametrize(
    ("observed_identity", "reread_complete", "evidence_mode", "expected"),
    [
        ("request_hash", True, "local_only", "uncertain"),
        ("request_hash", True, "external", "confirmed"),
        ("conflict", True, "external", "uncertain"),
        (None, False, "unavailable", "uncertain"),
    ],
    ids=[
        "local-hash-only",
        "matching-external-reread",
        "conflicting-reread",
        "unavailable-reread",
    ],
)
def test_non_create_confirmation_requires_independent_external_observation(
    workflow_db,
    observed_identity,
    reread_complete,
    evidence_mode,
    expected,
) -> None:
    factory, ids, context, task_id = workflow_db
    (event_id,) = seed_events(factory, ids, context, task_id)
    with session_scope(factory) as session:
        service = projection(session, ids)
        claim = service.claim_next(
            worker_id="worker", now=NOW, ttl=timedelta(minutes=1)
        )
        attempt = service.begin_attempt(
            event_id=event_id,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="worker",
            request_identity="external-observation-boundary",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW,
        )
        actual_identity = (
            attempt.request_sha256
            if observed_identity == "request_hash"
            else observed_identity
        )
        evidence = (
            {}
            if evidence_mode == "local_only"
            else external_evidence(available=False)
            if evidence_mode == "unavailable"
            else external_evidence(observed_identity=actual_identity)
        )
        result = service.record_observation_and_adjudicate(
            attempt_id=attempt.attempt_id,
            observation_kind="reread",
            observed_applied=True if reread_complete else None,
            observed_identity=actual_identity,
            reread_complete=reread_complete,
            evidence=evidence,
            decided_by="automatic",
            decision_reason="boundary test",
            observed_at=NOW,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="worker",
        )
        assert result.outcome == expected
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == (
            "applied" if expected == "confirmed" else "uncertain"
        )
