"""Native PostgreSQL command-process loss, replay, and disconnect evidence."""
from __future__ import annotations

import os
import uuid

import pytest

from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_pg.workflow import sha256_json
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db
from tests.support.postgresql.process_failure import (
    BarrierServer,
    command_snapshot,
    compose_control,
    read_command_result,
    start_command_process,
    write_scenario,
)
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _next, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _seed_command_authority(core_db) -> tuple[object, uuid.UUID, uuid.UUID]:
    factory, ids, context, _task_id = native_workflow_db(core_db)
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="section1 command-process rehearsal",
            created_at=NOW,
            external_effects_enabled=True,
        )
    return factory, run_id, request_id


def _stored_outcome_view(result: dict) -> dict:
    return {
        key: result[key]
        for key in ("ok", "command", "code", "http_status", "data", "retryable")
    }


def _assert_one_create(snapshot: dict) -> None:
    assert snapshot["request_count"] == 1
    assert snapshot["outcome_count"] == 1
    assert snapshot["execution_count"] == 1
    assert snapshot["execution_statuses"] == ["committed"]
    assert snapshot["task_count"] == 1
    assert snapshot["content_versions"] == 1
    assert snapshot["membership_events"] == 1
    assert snapshot["placement_events"] == 1
    assert snapshot["completion_events"] == 1
    assert snapshot["projection_events"] == 1


def test_command_process_commit_before_response_replays_without_duplicate_mutation(
    core_db, tmp_path
) -> None:
    factory, run_id, request_id = _seed_command_authority(core_db)
    lost_output = tmp_path / "lost-command-response.json"
    title = "Section 1 committed command"

    with BarrierServer() as barrier:
        first = start_command_process(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            run_id=run_id,
            request_id=request_id,
            output=lost_output,
            now=NOW,
            arguments={"title": title},
            scenario="after_commit_before_response",
            barrier=barrier,
        )
        reached = barrier.wait("after_authoritative_commit_before_response")
        committed_result = dict(reached.payload["result"])
        assert reached.payload["result_sha256"] == sha256_json(committed_result)
        committed_snapshot = command_snapshot(factory, request_id=request_id)
        _assert_one_create(committed_snapshot)
        assert committed_result["request_replayed"] is False
        first_exit = first.kill()
        reached.close()

    assert first_exit != 0
    assert not lost_output.exists()

    replay_output = tmp_path / "replayed-command-response.json"
    replay = start_command_process(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        run_id=run_id,
        request_id=request_id,
        output=replay_output,
        now=NOW,
        arguments={"title": title},
        scenario="normal",
    )
    replay.wait()
    replay_result = dict(read_command_result(replay_output)["result"])
    replayed_snapshot = command_snapshot(factory, request_id=request_id)

    assert replay_result["request_replayed"] is True
    assert _stored_outcome_view(replay_result) == _stored_outcome_view(committed_result)
    assert replayed_snapshot == committed_snapshot
    _assert_one_create(replayed_snapshot)
    write_scenario(
        "command-commit-before-response-replay",
        {
            "terminated_process_exit_code": first_exit,
            "lost_response_file_exists": lost_output.exists(),
            "committed_result": committed_result,
            "replay_result": replay_result,
            "committed_snapshot": committed_snapshot,
            "replayed_snapshot": replayed_snapshot,
        },
        nodeid=(
            "tests/postgresql/native/test_process_failure_command.py::"
            "test_command_process_commit_before_response_replays_without_duplicate_mutation"
        ),
        tmp_path=tmp_path,
    )


@pytest.mark.skipif(
    not os.environ.get("DISH_SECTION1_COMPOSE_JSON"),
    reason=(
        "requires DISH_SECTION1_COMPOSE_JSON compose control for the shared TEST "
        "PostgreSQL target; no runner currently provides this under bare native "
        "certification (see docs/testing.md, '§1 process-failure rehearsal' section) "
        "— waived pending dedicated wiring, already covered via dish-pg-process-failure"
    ),
)
def test_command_process_disconnect_before_commit_fails_closed_and_recovers(
    core_db, tmp_path
) -> None:
    factory, run_id, request_id = _seed_command_authority(core_db)
    failed_output = tmp_path / "disconnected-command-response.json"
    title = "Section 1 disconnected command"
    postgres_stopped = False
    barrier_released = False
    failure_code: int | None = None

    with BarrierServer() as barrier:
        child = start_command_process(
            dsn=postgresql_dsn(),
            tmp_path=tmp_path,
            run_id=run_id,
            request_id=request_id,
            output=failed_output,
            now=NOW,
            arguments={"title": title},
            scenario="after_execution_before_commit",
            barrier=barrier,
        )
        reached = barrier.wait("after_command_execution_before_commit")
        uncommitted_result = dict(reached.payload["result"])
        try:
            compose_control("stop")
            postgres_stopped = True
            reached.release()
            barrier_released = True
            failure_code = child.wait(expected=None)
        finally:
            if not barrier_released:
                reached.close()
                child.kill()
            if postgres_stopped:
                compose_control("start")
                postgres_stopped = False

    assert failure_code is not None
    assert failure_code != 0
    failure_result = read_command_result(failed_output, expected_status="error")
    factory.kw["bind"].dispose()
    after_disconnect = command_snapshot(factory, request_id=request_id)
    assert after_disconnect["request_count"] == 0
    assert after_disconnect["outcome_count"] == 0
    assert after_disconnect["execution_count"] == 0
    assert after_disconnect["task_count"] == 0
    assert after_disconnect["projection_events"] == 0

    recovered_output = tmp_path / "recovered-command-response.json"
    replacement = start_command_process(
        dsn=postgresql_dsn(),
        tmp_path=tmp_path,
        run_id=run_id,
        request_id=request_id,
        output=recovered_output,
        now=NOW,
        arguments={"title": title},
        scenario="normal",
    )
    replacement.wait()
    recovered_result = dict(read_command_result(recovered_output)["result"])
    after_recovery = command_snapshot(factory, request_id=request_id)

    assert recovered_result["ok"] is True
    assert recovered_result["request_replayed"] is False
    assert recovered_result["data"]["request_id"] == str(request_id)
    _assert_one_create(after_recovery)
    write_scenario(
        "command-disconnect-active-transaction",
        {
            "command_process_exit_code": failure_code,
            "uncommitted_result": uncommitted_result,
            "failure_result": failure_result,
            "after_disconnect": after_disconnect,
            "recovered_result": recovered_result,
            "after_recovery": after_recovery,
        },
        nodeid=(
            "tests/postgresql/native/test_process_failure_command.py::"
            "test_command_process_disconnect_before_commit_fails_closed_and_recovers"
        ),
        tmp_path=tmp_path,
    )
