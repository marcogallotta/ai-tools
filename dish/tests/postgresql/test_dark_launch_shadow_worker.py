from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import (
    ShadowIdentityMappingError,
    ShadowWorker,
    _translate_workflow_identifiers,
)
from dish_pg.transition import ShadowService
from dish_pg.workflow import sha256_json
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.workflow import NOW, _next, workflow_db


class Evaluator:
    def evaluate(self, session, envelope):
        del session
        return dict(envelope.source_outcome)


def _spool(tmp_path, *, treatment="execute"):
    spool=ShadowSpool(tmp_path/"spool.sqlite3")
    reservation=spool.reserve(
        source_request_identity=f"request-{treatment}", source_authority_generation="legacy-1",
        command_name="prepare", treatment=treatment,
        canonical_input={"command":"prepare","arguments":{}}, principal={},
        source_pre_state={"phase":"research"}, pinned_inputs={"rollout_mode":"execute"}, created_at=NOW,
    )
    spool.complete(reservation.registration_id, source_outcome={"ok":True},
                   source_post_state={"phase":"verification"}, source_effects={}, completed_at=NOW)
    return spool


def test_shadow_worker_delivers_executes_and_compares(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline=ShadowService(session, uuid_factory=lambda:_next(ids)).create_baseline(
            generation_id=context["generation_id"], source_generation_identity="legacy-1",
            source_commit="worktree", created_at=NOW)
        baseline_id=baseline.shadow_baseline_id
    spool=_spool(tmp_path)
    worker=ShadowWorker(spool=spool, session_maker=factory, baseline_id=baseline_id,
                        evaluator=Evaluator(), worker_id="shadow-1", comparator_release="test",
                        kill_switch_path=tmp_path/"dark-launch.disabled", clock=lambda:NOW)
    assert worker.run_once() is True
    with session_scope(factory) as session:
        comparison=session.scalar(select(tx.ShadowComparison))
        assert comparison.parity_class == "exact"
    assert spool.status()["counts"]["delivered"] == 1


def test_capture_only_is_settled_as_explicit_uncomparable_gap(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline=ShadowService(session, uuid_factory=lambda:_next(ids)).create_baseline(
            generation_id=context["generation_id"], source_generation_identity="legacy-1",
            source_commit="worktree", created_at=NOW)
        baseline_id=baseline.shadow_baseline_id
    worker=ShadowWorker(spool=_spool(tmp_path,treatment="capture_only"), session_maker=factory,
                        baseline_id=baseline_id, evaluator=Evaluator(), worker_id="shadow-1",
                        comparator_release="test", kill_switch_path=tmp_path/"dark-launch.disabled",
                        clock=lambda:NOW)
    worker.run_once()
    with session_scope(factory) as session:
        assert session.scalar(select(tx.ShadowComparison)).parity_class == "gap"
        assert session.scalar(select(tx.ShadowGap)).gap_kind == "uncomparable"


def test_kill_switch_stops_worker_before_spool_delivery(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id

    spool = _spool(tmp_path)
    kill_switch = tmp_path / "dark-launch.disabled"
    kill_switch.write_text("disabled\n")
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=Evaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=kill_switch,
        clock=lambda: NOW,
    )

    assert worker.run_once() is False
    assert spool.status()["counts"]["complete"] == 1
    with session_scope(factory) as session:
        assert session.scalar(select(tx.ShadowEnvelope)) is None


def test_real_shadow_evaluator_tags_outbox_and_projection_claim_refuses_it(workflow_db):
    from datetime import timedelta

    from dish_pg.shadow_worker import CommandPortShadowEvaluator
    from dish_pg.transition import ProjectionService

    factory, ids, context, _task = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="prove shadow origin isolation",
            created_at=NOW,
            external_effects_enabled=True,
        )
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        envelope = ShadowService(session, uuid_factory=lambda: _next(ids)).capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="create",
            source_request_identity=str(request_id),
            canonical_input={"command": "create", "arguments": {"title": "Shadow only"}},
            source_outcome={"ok": True},
            source_post_state={"captured": True},
            principal={
                "owner_id": "owner-1",
                "principal_class": "agent",
                "run_id": str(run_id),
            },
            pinned_inputs={"rollout_mode": "execute"},
            capture_qualification="execute",
            captured_at=NOW,
        )

        target = CommandPortShadowEvaluator(cursor_secret=b"shadow-test-cursor-secret-32bytes!").evaluate(
            session, envelope
        )
        event = session.scalar(select(tx.ProjectionOutboxEvent))
        registered_run = session.scalar(select(wf.ServiceRun))
        request = session.scalar(select(wf.ServiceRequest))

        assert target["ok"] is True
        assert registered_run is not None
        assert registered_run.run_id != run_id
        assert registered_run.owner_id == "owner-1"
        assert registered_run.agent == "service"
        assert request is not None
        assert request.run_id == registered_run.run_id
        assert request.request_id != request_id
        assert epoch.external_effects_enabled is True
        assert event is not None
        assert event.origin == "shadow"
        assert event.state == "pending"
        assert projection.claim_next(
            worker_id="projection-worker",
            now=NOW,
            ttl=timedelta(minutes=2),
        ) is None


def test_shadow_identifier_translation_uses_prior_comparison_bindings(workflow_db):
    factory, ids, context, _task = workflow_db
    source_operation, target_operation = uuid.uuid4(), uuid.uuid4()
    source_lease, target_lease = uuid.uuid4(), uuid.uuid4()
    with session_scope(factory) as session:
        service = ShadowService(session, uuid_factory=lambda: _next(ids))
        baseline = service.create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        prior = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="start",
            source_request_identity="source-start",
            canonical_input={"command": "start", "arguments": {}},
            source_outcome={
                "submission_id": str(source_operation),
                "data": {"operation_id": str(source_operation), "lease_id": str(source_lease)},
            },
            source_post_state={"phase": "research"},
            rollout_sequence=1,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        target_result = {
            "ok": True,
            "command": "start",
            "code": "OK",
            "http_status": 200,
            "data": {"operation_id": str(target_operation), "lease_id": str(target_lease)},
            "retryable": False,
        }
        session.add(
            tx.ShadowComparison(
                comparison_id=_next(ids),
                envelope_id=prior.envelope_id,
                target_result=target_result,
                target_result_sha256=sha256_json(target_result),
                parity_class="mismatch",
                differences=[],
                comparator_release="test",
                compared_at=NOW,
            )
        )
        current = service.capture_envelope(
            shadow_baseline_id=baseline.shadow_baseline_id,
            command_name="renew-lease",
            source_request_identity="source-renew",
            canonical_input={"command": "renew-lease", "arguments": {}},
            source_outcome={"ok": True},
            source_post_state={"phase": "research"},
            rollout_sequence=2,
            source_authority_generation="legacy-1",
            captured_at=NOW,
        )
        translated = _translate_workflow_identifiers(
            session,
            current,
            {"submission_id": str(source_operation), "lease_id": str(source_lease)},
        )
        assert translated == {
            "submission_id": str(target_operation),
            "lease_id": str(target_lease),
        }
        with pytest.raises(ShadowIdentityMappingError, match="no unique target operation"):
            _translate_workflow_identifiers(
                session,
                current,
                {"submission_id": str(uuid.uuid4())},
            )


def test_shadow_worker_compacts_delivered_payload_after_retention(workflow_db, tmp_path):
    from datetime import timedelta

    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
    spool = _spool(tmp_path)
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=Evaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=tmp_path / "dark-launch.disabled",
        delivered_retention=timedelta(0),
        clock=lambda: NOW,
    )
    assert worker.run_once() is True
    assert spool.status()["counts"]["delivered"] == 0
    assert spool.status()["counts"]["archived"] == 1


def test_shadow_worker_recovers_stale_reservation_before_later_delivery(workflow_db, tmp_path):
    from datetime import timedelta

    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=lambda: _next(ids)).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="legacy-1",
            source_commit="worktree",
            created_at=NOW,
        )
        baseline_id = baseline.shadow_baseline_id
    spool = ShadowSpool(tmp_path / "spool.sqlite3", min_free_bytes=1)
    first = spool.reserve(
        source_request_identity="request-first",
        source_authority_generation="legacy-1",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {}},
        principal={},
        source_pre_state={},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW,
    )
    second = spool.reserve(
        source_request_identity="request-second",
        source_authority_generation="legacy-1",
        command_name="prepare",
        treatment="execute",
        canonical_input={"command": "prepare", "arguments": {}},
        principal={},
        source_pre_state={},
        pinned_inputs={"rollout_mode": "execute"},
        created_at=NOW,
    )
    spool.complete(
        second.registration_id,
        source_outcome={"ok": True},
        source_post_state={"phase": "verification"},
        source_effects={},
        completed_at=NOW,
    )
    worker = ShadowWorker(
        spool=spool,
        session_maker=factory,
        baseline_id=baseline_id,
        evaluator=Evaluator(),
        worker_id="shadow-1",
        comparator_release="test",
        kill_switch_path=tmp_path / "dark-launch.disabled",
        reservation_ttl=timedelta(seconds=90),
        clock=lambda: NOW + timedelta(seconds=91),
    )
    assert worker.run_once() is True
    status = spool.status()["counts"]
    assert status["delivered"] == 1
    assert status["complete"] == 1
    with session_scope(factory) as session:
        gap = session.scalar(select(tx.ShadowGap))
        assert gap is not None
        assert gap.gap_identity == "capture:request-first"
    assert first.rollout_sequence < second.rollout_sequence
