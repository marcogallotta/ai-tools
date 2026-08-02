from __future__ import annotations

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.abandonment import settle_abandonment_frontier
from dish_tool.database import create_abandonment_attempt_in_transaction
from tests.support.workflow_builders import create_large_rejection_successor, reject_large
from tests.support.verification import TASK, make_app


def _settle_targeted_verification_continuation(app, backend, tmp_path, operation_id):
    review = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="dead-verifier-run",
        independence_attestation="independent",
    )
    old_cycle_id = review["data"]["cycle_id"]
    next_cycle = create_large_rejection_successor(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="codex",
        run_id="dead-verifier-run",
        candidate_text=TASK.replace("100 g", "120 g"),
    )
    lease = LeaseManager(app.conn).acquire(
        operation_id,
        ServicePrincipal("owner", "dead-verifier-run"),
        context_cycle_id=old_cycle_id,
    )
    LeaseManager(app.conn).release(
        operation_id, None, reason="stale verifier released", admin=True
    )
    app.conn.execute("BEGIN IMMEDIATE")
    create_abandonment_attempt_in_transaction(
        app.conn,
        abandonment_id="abandonment",
        task_gid="t",
        source_operation_id=operation_id,
        source_lease_id=lease["lease_id"],
        abandoned_owner_id="owner",
        abandoned_run_id="dead-verifier-run",
        attempt_cycle_id=old_cycle_id,
        reason="conversation permanently unavailable",
    )
    app.conn.execute("COMMIT")
    result = settle_abandonment_frontier(
        app.conn, backend, abandonment_id="abandonment", reason="preserve route"
    )
    assert result["required_action"]["arguments"] == {
        "task_gid": "t",
        "kind": "verification",
        "target_operation_id": operation_id,
        "target_cycle_id": next_cycle["cycle_id"],
    }
    return next_cycle


def _claim_targeted_continuation(app, backend, tmp_path, operation_id, next_cycle):
    claimed = app.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="verification",
        run_id="fresh-run",
        independence_attestation="independent",
        target_operation_id=operation_id,
        target_cycle_id=next_cycle["cycle_id"],
    )
    assert claimed["ok"], claimed
    live_candidate = f"{backend.title}\n{backend.notes}".replace("120 g", "130 g")
    held = reject_large(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="gpt",
        run_id="fresh-run",
        candidate_text=live_candidate,
    )
    assert held["data"]["verification_hold"] is False
    next_cycle_id = held["data"]["new_cycle_id"]
    assert next_cycle_id
    claimed = app.execute(
        "start",
        agent="claude",
        task_gid="t",
        kind="verification",
        run_id="third-run",
        independence_attestation="independent",
        target_operation_id=operation_id,
        target_cycle_id=next_cycle_id,
    )
    assert claimed["ok"], claimed
    held_candidate = f"{backend.title}\n{backend.notes}".replace("130 g", "140 g")
    held = reject_large(
        app,
        tmp_path,
        operation_id=operation_id,
        agent="claude",
        run_id="third-run",
        candidate_text=held_candidate,
    )
    assert held["data"]["verification_hold"] is True


def _reopen_route_and_assert_old_target_is_stale(
    app, backend, tmp_path, operation_id, next_cycle
):
    reopened_text = f"{backend.title}\n{backend.notes}".replace(
        "Compare hydration routes.",
        "Compare hydration routes after a rested-starch reset.",
    )
    reopened_candidate = tmp_path / "reopened-route.txt"
    reopened_candidate.write_text(reopened_text, encoding="utf-8")
    reopened = DishAdminApplication(app.conn, backend=backend).execute(
        "reopen",
        submission_id=operation_id,
        category="premise",
        before="Compare hydration routes.",
        after="Compare hydration routes after a rested-starch reset.",
        editor="codex",
        model="gpt-5.6-sol",
        run_id="reopen-run",
        file_path=str(reopened_candidate),
        date="2026-07-31",
    )
    assert reopened["ok"], reopened
    later = app.conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()
    assert later is not None
    stale = app.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="later-run",
        independence_attestation="independent",
        target_operation_id=operation_id,
        target_cycle_id=next_cycle["cycle_id"],
    )
    assert stale["errors"][0]["rule"] == "verification_start_target_stale"
    assert app.conn.execute(
        "SELECT run_id FROM verification_cycles WHERE cycle_id=?", (later["cycle_id"],)
    ).fetchone()[0] is None


@pytest.mark.smoke
def test_route_preserved_verification_continuation_is_exact_targeted(tmp_path):
    app, backend, operation_id, _ = make_app(tmp_path)
    next_cycle = _settle_targeted_verification_continuation(
        app, backend, tmp_path, operation_id
    )
    assert next_cycle["cycle_id"]
    _claim_targeted_continuation(
        app, backend, tmp_path, operation_id, next_cycle
    )
    _reopen_route_and_assert_old_target_is_stale(
        app, backend, tmp_path, operation_id, next_cycle
    )

