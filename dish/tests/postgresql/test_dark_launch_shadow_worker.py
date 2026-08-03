from __future__ import annotations

from sqlalchemy import select

from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_worker import ShadowWorker
from dish_pg.transition import ShadowService
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
