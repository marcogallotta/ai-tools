from __future__ import annotations

from dish_pg.dark_launch import status
from dish_pg.database import session_scope
from dish_pg.transition import ShadowService
from dish_service.shadow_spool import ShadowSpool
from tests.support.postgresql.workflow import NOW, _next, workflow_db


def test_dark_launch_status_reports_spool_baseline_and_effect_gate(workflow_db, tmp_path):
    factory, ids, context, _task = workflow_db
    with session_scope(factory) as session:
        baseline=ShadowService(session, uuid_factory=lambda:_next(ids)).create_baseline(
            generation_id=context["generation_id"], source_generation_identity="legacy-1",
            source_commit="worktree", created_at=NOW)
        baseline_id=baseline.shadow_baseline_id
    report=status(session_maker=factory, spool=ShadowSpool(tmp_path/"spool.sqlite3"), baseline_id=baseline_id)
    assert report["baseline"]["shadow_baseline_id"] == str(baseline_id)
    assert report["baseline"]["delivery_counts"]["pending"] == 0
    assert report["spool"]["counts"]["complete"] == 0
