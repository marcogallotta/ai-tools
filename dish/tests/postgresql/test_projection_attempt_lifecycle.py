from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from dish_pg.workflow import sha256_json
from tests.support.postgresql.projection_attempts import (
    external_evidence,
    projection,
    seed_events,
)
from tests.support.postgresql.workflow import NOW, workflow_db


def _reproject_attempt(workflow_db):
    factory, ids, context, task_id = workflow_db
    authoritative_snapshot = {
        "notes": "canonical",
        "section_id": str(context["section_id"]),
        "completed": False,
    }
    with session_scope(factory) as session:
        service = projection(session, ids)
        service.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="reproject adjudication",
            created_at=NOW,
            external_effects_enabled=True,
        )
        service.bind_imported_mappings(
            generation_id=context["generation_id"],
            bound_at=NOW,
        )
        mapping = session.scalar(
            select(tx.TaskProjectionMapping).where(tx.TaskProjectionMapping.task_id == task_id)
        )
        drift = service.record_drift_and_reproject(
            generation_id=context["generation_id"],
            task_id=task_id,
            task_mapping_id=mapping.mapping_id,
            drift_kind="document",
            external_snapshot={"notes": "edited outside Dish"},
            authoritative_snapshot=authoritative_snapshot,
            evidence={"scan": "reproject-adjudication"},
            detected_at=NOW,
        )
        claim = service.claim_next(
            worker_id="reproject-worker",
            now=NOW,
            ttl=timedelta(minutes=1),
        )
        attempt = service.begin_attempt(
            event_id=drift.reproject_event_id,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="reproject-worker",
            request_identity="reproject-request",
            request_payload={"notes": "canonical"},
            intended_external_id="123456789",
            started_at=NOW,
        )
    return factory, drift.reproject_event_id, claim, attempt, sha256_json(authoritative_snapshot)


def _reproject_evidence(*, identity=None, external_id="123456789", absent=False, source="external_reread"):
    fact = {
        "source": source,
        "operation": "reproject",
        "observed_external_id": external_id,
    }
    if identity is not None:
        fact["observed_reproject_state_identity"] = identity
    if absent:
        fact["observed_absent"] = True
    return {"external_observation": fact}


def _settle_reproject(
    factory,
    event_id,
    claim,
    attempt,
    *,
    observed_applied,
    observed_identity,
    evidence,
):
    with session_scope(factory) as session:
        result = projection(session).record_observation_and_adjudicate(
            attempt_id=attempt.attempt_id,
            observation_kind="reread",
            observed_applied=observed_applied,
            observed_identity=observed_identity,
            reread_complete=True,
            evidence=evidence,
            decided_by="automatic",
            decision_reason="reproject external reread",
            observed_at=NOW,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="reproject-worker",
        )
        return result.outcome, session.get(tx.ProjectionOutboxEvent, event_id).state


def test_reproject_matching_independent_state_reread_confirms_applied(workflow_db) -> None:
    factory, event_id, claim, attempt, state_identity = _reproject_attempt(workflow_db)
    outcome, state = _settle_reproject(
        factory,
        event_id,
        claim,
        attempt,
        observed_applied=True,
        observed_identity=state_identity,
        evidence=_reproject_evidence(identity=state_identity),
    )
    assert outcome == "confirmed"
    assert state == "applied"


def test_reproject_verified_external_absence_remains_not_applied(workflow_db) -> None:
    factory, event_id, claim, attempt, _state_identity = _reproject_attempt(workflow_db)
    outcome, state = _settle_reproject(
        factory,
        event_id,
        claim,
        attempt,
        observed_applied=False,
        observed_identity=None,
        evidence=_reproject_evidence(absent=True),
    )
    assert outcome == "not_applied"
    assert state == "pending"


@pytest.mark.parametrize(
    ("observed_identity", "evidence_factory"),
    [
        pytest.param("state", lambda state: {}, id="missing-evidence"),
        pytest.param(
            "state",
            lambda state: {"external_observation": "malformed"},
            id="malformed-evidence",
        ),
        pytest.param(
            "state",
            lambda state: _reproject_evidence(identity=state, external_id="987654321"),
            id="mismatched-target",
        ),
        pytest.param(
            "mismatch",
            lambda state: _reproject_evidence(identity="mismatch"),
            id="mismatched-state-identity",
        ),
        pytest.param(
            "stale",
            lambda state: _reproject_evidence(identity="stale"),
            id="stale-state-identity",
        ),
        pytest.param(
            "state",
            lambda state: _reproject_evidence(identity=state, source="local_cache"),
            id="non-independent-source",
        ),
    ],
)
def test_reproject_missing_mismatched_or_non_independent_evidence_stays_uncertain(
    workflow_db, observed_identity, evidence_factory
) -> None:
    factory, event_id, claim, attempt, state_identity = _reproject_attempt(workflow_db)
    actual_identity = state_identity if observed_identity == "state" else observed_identity
    outcome, state = _settle_reproject(
        factory,
        event_id,
        claim,
        attempt,
        observed_applied=True,
        observed_identity=actual_identity,
        evidence=evidence_factory(state_identity),
    )
    assert outcome == "uncertain"
    assert state == "uncertain"


@pytest.mark.parametrize(
    ("event_type", "identity_field"),
    [
        ("update_task_document", "observed_document_identity"),
        ("move_task", "observed_membership_identity"),
        ("set_completion", "observed_completion_identity"),
    ],
)
def test_existing_non_create_external_observation_identity_contract_is_unchanged(
    event_type, identity_field
) -> None:
    identity = "existing-operation-identity"
    assert ProjectionService._is_independent_external_observation(
        event=SimpleNamespace(event_type=event_type, intent_payload={}),
        attempt=SimpleNamespace(intended_external_id="123456789"),
        observation_kind="reread",
        observed_applied=True,
        observed_identity=identity,
        evidence={
            "external_observation": {
                "source": "external_reread",
                "operation": event_type,
                "observed_external_id": "123456789",
                identity_field: identity,
            }
        },
    )


def test_existing_create_external_marker_contract_is_unchanged() -> None:
    marker = "dish-correlation-marker"
    assert ProjectionService._is_independent_external_observation(
        event=SimpleNamespace(
            event_type="create_task", intent_payload={"correlation_marker": marker}
        ),
        attempt=SimpleNamespace(intended_external_id=None),
        observation_kind="marker_search",
        observed_applied=True,
        observed_identity=marker,
        evidence={
            "external_observation": {
                "source": "external_marker_search",
                "operation": "create_task",
                "correlation_marker": marker,
            }
        },
    )


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
