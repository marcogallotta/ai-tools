"""Native process takeover, lease eligibility, fencing, and task-local concurrency."""
from __future__ import annotations

from datetime import timedelta

import pytest

from dish_pg.database import session_scope
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import _import_one, core_db
from tests.support.postgresql.process_failure import (
    BarrierServer,
    event_snapshot,
    expire_claim,
    read_ledger,
    start_projection_worker,
    write_scenario,
)
from tests.support.postgresql.projection_attempts import native_workflow_db, projection, seed_events
from tests.support.postgresql.workflow import NOW, _claimed_execution

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _seed_independent_events(core_db):
    factory, ids, context, first_task_id = native_workflow_db(core_db)
    first_event = seed_events(factory, ids, context, first_task_id)[0]
    with session_scope(factory) as session:
        second_task = _import_one(session, ids, context, asana_gid="123456790")
        execution_id = _claimed_execution(session, ids, context, second_task.task_id)
        second_event = projection(session, ids).record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=second_task.task_id,
            event_type="update_task_document",
            payload={"content_version_id": "independent-v2"},
            created_at=NOW + timedelta(seconds=1),
        )
    return factory, first_event, second_event


def test_process_takeover_is_lease_gated_fenced_and_task_local(core_db, tmp_path) -> None:
    factory, claimed_event, independent_event = _seed_independent_events(core_db)
    events = [claimed_event, independent_event]
    ledger = tmp_path / "external-ledger.json"
    with BarrierServer() as barrier:
        old = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="takeover-old",
            scenario="after_claim",
            barrier=barrier,
        )
        reached_old = barrier.wait("after_claim_before_durable_intent")
        claimed = event_snapshot(factory, events)
        old_event = claimed["events"][0]
        assert old_event["state"] == "claimed"
        assert old_event["claim_owner"] == "takeover-old"

        independent = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="takeover-independent",
        )
        independent.wait()
        independent_done = event_snapshot(factory, events)
        assert [row["state"] for row in independent_done["events"]] == ["claimed", "applied"]
        assert read_ledger(ledger)["dispatch_calls"] == 1

        too_early = start_projection_worker(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            ledger=ledger,
            worker_id="takeover-too-early",
        )
        too_early.wait()
        assert event_snapshot(factory, events) == independent_done

        expire_claim(factory, claimed_event)
        with BarrierServer() as replacement_barrier:
            replacement = start_projection_worker(
                dsn=postgresql_dsn(),
                tmp_path=tmp_path,
                ledger=ledger,
                worker_id="takeover-new",
                scenario="after_claim",
                barrier=replacement_barrier,
            )
            reached_new = replacement_barrier.wait("after_claim_before_durable_intent")
            taken_over = event_snapshot(factory, events)
            assert taken_over["events"][0]["claim_owner"] == "takeover-new"
            assert taken_over["events"][0]["claim_revision"] > old_event["claim_revision"]

            reached_old.release()
            old.wait()
            assert "projection claim lost mid-processing" in old.log_path.read_text(
                encoding="utf-8"
            )
            fenced = event_snapshot(factory, events)
            assert fenced == taken_over
            assert all(row["worker_id"] != "takeover-old" for row in fenced["attempts"])

            reached_new.release()
            replacement.wait()

    final = event_snapshot(factory, events)
    external = read_ledger(ledger)
    assert [row["state"] for row in final["events"]] == ["applied", "applied"]
    assert external["dispatch_calls"] == 2
    assert all(row["worker_id"] != "takeover-old" for row in final["attempts"])
    write_scenario(
        "worker-takeover-and-fencing",
        {
            "claimed": claimed,
            "independent_done": independent_done,
            "taken_over": taken_over,
            "final": final,
            "external": external,
        },
        nodeid="tests/postgresql/native/test_process_failure_takeover.py::test_process_takeover_is_lease_gated_fenced_and_task_local",
        tmp_path=tmp_path,
    )
