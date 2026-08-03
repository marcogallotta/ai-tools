from __future__ import annotations

from sqlalchemy import select

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
                        clock=lambda:NOW)
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
                        comparator_release="test", clock=lambda:NOW)
    worker.run_once()
    with session_scope(factory) as session:
        assert session.scalar(select(tx.ShadowComparison)).parity_class == "gap"
        assert session.scalar(select(tx.ShadowGap)).gap_kind == "uncomparable"
