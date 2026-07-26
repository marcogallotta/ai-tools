import json
import os
import socket
from datetime import datetime, timedelta, timezone

import pytest

from dish_tool import admin_cli, cli
from dish_tool.admin import DishAdminApplication
from dish_tool.constants import RECOVERY_QUARANTINE_SECONDS, SUBMISSION_STATES
from dish_tool.database import get_submission, initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.models import ProcessIdentity
from dish_tool.recovery import (
    current_process_identity,
    finish_write_attempt,
    process_identity_is_live,
)

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
RECOVERABLE_STATES = {"in_flight", "uncertain"}
DISCARDABLE_STATES = {
    "drafting",
    "research_handoff",
    "awaiting_verification",
    "awaiting_human",
    "ready",
    "written",
}


def timestamp(moment):
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def insert_submission(
    conn,
    submission_id,
    status,
    *,
    task_gid=None,
    attempt_id="attempt-old",
    in_flight_at=None,
    hostname="dead-host",
    pid=99999999,
    process_start="old-process",
):
    task_gid = task_gid or f"task-{submission_id}"
    attempt_fields = status in RECOVERABLE_STATES
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, status, write_attempt_id,
            in_flight_at, in_flight_hostname, in_flight_pid,
            in_flight_process_start, created_at
        ) VALUES (?, ?, 'initial', 'fixture-v1', 'fixture-commit', '{}', '{}',
                  'claude', 'claude', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            submission_id,
            task_gid,
            status,
            attempt_id if attempt_fields else None,
            in_flight_at if attempt_fields else None,
            hostname if attempt_fields else None,
            pid if attempt_fields else None,
            process_start if attempt_fields else None,
            timestamp(NOW - timedelta(hours=1)),
        ),
    )
    return submission_id


def make_admin(tmp_path, *, live=False):
    conn = initialize_database(tmp_path / "dish.db")
    app = DishAdminApplication(
        conn,
        now_provider=lambda: NOW,
        process_liveness_checker=lambda identity: live,
    )
    return app


def old_enough():
    return timestamp(NOW - timedelta(seconds=RECOVERY_QUARANTINE_SECONDS))


def audit_row(app, event_type):
    row = app.conn.execute(
        """
        SELECT submission_id, task_gid, actor_agent, details
          FROM audit_events
         WHERE event_type = ?
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (event_type,),
    ).fetchone()
    assert row is not None
    return row, json.loads(row["details"])


@pytest.mark.parametrize("source_state", sorted(RECOVERABLE_STATES))
@pytest.mark.parametrize(
    ("outcome", "target_state"),
    [("not-applied", "ready"), ("applied", "written")],
)
def test_recover_accepts_only_stuck_write_states_and_maps_outcome(
    tmp_path, source_state, outcome, target_state
):
    app = make_admin(tmp_path)
    sid = insert_submission(
        app.conn, "recoverable", source_state, in_flight_at=old_enough()
    )

    result = app.execute(
        "recover",
        submission_id=sid,
        outcome=outcome,
        reason="inspected the live task notes",
    )

    assert result["ok"] is True
    assert result["state"] == target_state
    saved = get_submission(app.conn, sid)
    assert saved["status"] == target_state
    assert saved["write_attempt_id"] is None
    assert saved["in_flight_at"] is None
    assert saved["in_flight_hostname"] is None
    assert saved["in_flight_pid"] is None
    assert saved["in_flight_process_start"] is None
    if outcome == "applied":
        assert saved["task_content_written_at"] is not None
    else:
        assert saved["task_content_written_at"] is None
    row, details = audit_row(app, "dish-admin.recover")
    assert row["submission_id"] == sid
    assert row["task_gid"] == saved["task_gid"]
    assert row["actor_agent"] is None
    assert details["actor_role"] == "marco"
    assert details["prior_state"] == source_state
    assert details["outcome"] == outcome
    assert details["reason"] == "inspected the live task notes"
    assert details["invalidated_write_attempt_id"] == "attempt-old"


@pytest.mark.parametrize("state", sorted(SUBMISSION_STATES - RECOVERABLE_STATES))
def test_recover_rejects_every_other_source_state(tmp_path, state):
    app = make_admin(tmp_path)
    sid = insert_submission(app.conn, f"recover-{state}", state)

    result = app.execute(
        "recover",
        submission_id=sid,
        outcome="not-applied",
        reason="inspected live notes",
    )

    assert result["code"] == "WRONG_STATE"
    assert result["state"] == state
    assert get_submission(app.conn, sid)["status"] == state


def test_recover_requires_concrete_outcome_and_reason(tmp_path):
    app = make_admin(tmp_path)
    sid = insert_submission(
        app.conn, "missing-input", "uncertain", in_flight_at=old_enough()
    )

    bad_outcome = app.execute(
        "recover", submission_id=sid, outcome="maybe", reason="inspected"
    )
    missing_reason = app.execute(
        "recover", submission_id=sid, outcome="applied", reason="  "
    )

    assert bad_outcome["code"] == "INVALID_ARGUMENT"
    assert bad_outcome["errors"][0]["rule"] == "invalid_recovery_outcome"
    assert missing_reason["code"] == "INVALID_ARGUMENT"
    assert missing_reason["errors"][0]["rule"] == "recovery_reason_required"
    assert get_submission(app.conn, sid)["status"] == "uncertain"


def test_recover_refuses_live_recorded_process(tmp_path):
    app = make_admin(tmp_path, live=True)
    sid = insert_submission(
        app.conn, "live", "in_flight", in_flight_at=old_enough()
    )

    result = app.execute(
        "recover",
        submission_id=sid,
        outcome="not-applied",
        reason="process check",
    )

    assert result["code"] == "CONFLICT"
    assert result["retryable"] is True
    assert result["errors"][0]["rule"] == "recovery_process_live"
    assert get_submission(app.conn, sid)["write_attempt_id"] == "attempt-old"


def test_recover_refuses_before_quarantine_and_accepts_exact_boundary(tmp_path):
    app = make_admin(tmp_path)
    too_new = insert_submission(
        app.conn,
        "too-new",
        "uncertain",
        in_flight_at=timestamp(
            NOW - timedelta(seconds=RECOVERY_QUARANTINE_SECONDS - 0.001)
        ),
    )
    boundary = insert_submission(
        app.conn, "boundary", "uncertain", attempt_id="attempt-boundary", in_flight_at=old_enough()
    )

    refused = app.execute(
        "recover",
        submission_id=too_new,
        outcome="not-applied",
        reason="checked live notes",
    )
    accepted = app.execute(
        "recover",
        submission_id=boundary,
        outcome="not-applied",
        reason="checked live notes",
    )

    assert refused["code"] == "CONFLICT"
    assert refused["retryable"] is True
    assert refused["errors"][0]["rule"] == "recovery_quarantine_active"
    assert accepted["state"] == "ready"


def test_process_identity_detects_live_dead_and_pid_reuse():
    current = current_process_identity()
    dead = ProcessIdentity(
        hostname=socket.gethostname(),
        pid=99999999,
        process_start="definitely-not-running",
    )
    reused = ProcessIdentity(
        hostname=socket.gethostname(),
        pid=os.getpid(),
        process_start="different-process-start",
    )

    assert process_identity_is_live(current) is True
    assert process_identity_is_live(dead) is False
    assert process_identity_is_live(reused) is False


def test_recover_command_handles_live_dead_and_pid_reuse_identities(tmp_path):
    conn = initialize_database(tmp_path / "identity.db")
    app = DishAdminApplication(conn, now_provider=lambda: NOW)
    current = current_process_identity()
    live_sid = insert_submission(
        conn,
        "command-live",
        "in_flight",
        attempt_id="attempt-live",
        in_flight_at=old_enough(),
        hostname=current.hostname,
        pid=current.pid,
        process_start=current.process_start,
    )
    dead_sid = insert_submission(
        conn,
        "command-dead",
        "in_flight",
        attempt_id="attempt-dead",
        in_flight_at=old_enough(),
        hostname=socket.gethostname(),
        pid=99999999,
        process_start="dead-process",
    )
    reused_sid = insert_submission(
        conn,
        "command-reused",
        "in_flight",
        attempt_id="attempt-reused",
        in_flight_at=old_enough(),
        hostname=socket.gethostname(),
        pid=os.getpid(),
        process_start="different-process-start",
    )

    live = app.execute(
        "recover",
        submission_id=live_sid,
        outcome="not-applied",
        reason="checked process identity",
    )
    dead = app.execute(
        "recover",
        submission_id=dead_sid,
        outcome="not-applied",
        reason="dead process and absent notes",
    )
    reused = app.execute(
        "recover",
        submission_id=reused_sid,
        outcome="not-applied",
        reason="PID belongs to a replacement process",
    )

    assert live["errors"][0]["rule"] == "recovery_process_live"
    assert dead["state"] == "ready"
    assert reused["state"] == "ready"


def test_recovery_invalidates_attempt_against_stale_completion(tmp_path):
    app = make_admin(tmp_path)
    sid = insert_submission(
        app.conn, "stale", "in_flight", in_flight_at=old_enough()
    )

    recovered = app.execute(
        "recover",
        submission_id=sid,
        outcome="not-applied",
        reason="notes were absent",
    )
    assert recovered["state"] == "ready"

    with pytest.raises(DishRuleError) as exc:
        finish_write_attempt(
            app.conn,
            sid,
            attempt_id="attempt-old",
            target_state="written",
        )
    assert exc.value.code == "CONFLICT"
    assert exc.value.rule == "stale_write_attempt"
    assert get_submission(app.conn, sid)["status"] == "ready"


@pytest.mark.parametrize("state", sorted(DISCARDABLE_STATES))
def test_discard_accepts_abandonable_states_and_releases_lock(tmp_path, state):
    app = make_admin(tmp_path)
    sid = insert_submission(app.conn, f"discard-{state}", state, task_gid="same-task")

    result = app.execute(
        "discard", submission_id=sid, reason="abandoned after manual review"
    )

    assert result["state"] == "discarded"
    saved = get_submission(app.conn, sid)
    assert saved["completed_at"] is not None
    # The partial unique lock is released by the terminal state.
    insert_submission(app.conn, f"replacement-{state}", "drafting", task_gid="same-task")
    row, details = audit_row(app, "dish-admin.discard")
    assert row["submission_id"] == sid
    assert row["task_gid"] == "same-task"
    assert details["prior_state"] == state
    assert details["reason"] == "abandoned after manual review"


@pytest.mark.parametrize(
    "state", sorted(SUBMISSION_STATES - DISCARDABLE_STATES)
)
def test_discard_rejects_in_flight_uncertain_and_terminal_states(tmp_path, state):
    app = make_admin(tmp_path)
    sid = insert_submission(
        app.conn,
        f"reject-discard-{state}",
        state,
        in_flight_at=old_enough(),
    )

    result = app.execute(
        "discard", submission_id=sid, reason="do not discard this state"
    )

    assert result["code"] == "WRONG_STATE"
    assert result["state"] == state
    assert get_submission(app.conn, sid)["status"] == state


def test_discard_requires_reason_and_never_uses_backend(tmp_path):
    app = make_admin(tmp_path)
    sid = insert_submission(app.conn, "discard-no-backend", "ready")

    class BackendBomb:
        def __getattr__(self, name):
            raise AssertionError(f"backend must not be accessed: {name}")

    app.backend = BackendBomb()
    missing = app.execute("discard", submission_id=sid, reason="  ")
    assert missing["code"] == "INVALID_ARGUMENT"
    assert get_submission(app.conn, sid)["status"] == "ready"

    result = app.execute(
        "discard", submission_id=sid, reason="manual abandonment decision"
    )
    assert result["state"] == "discarded"


def test_recover_never_uses_backend(tmp_path):
    app = make_admin(tmp_path)
    sid = insert_submission(
        app.conn, "recover-no-backend", "uncertain", in_flight_at=old_enough()
    )

    class BackendBomb:
        def __getattr__(self, name):
            raise AssertionError(f"backend must not be accessed: {name}")

    app.backend = BackendBomb()
    result = app.execute(
        "recover",
        submission_id=sid,
        outcome="applied",
        reason="notes are visibly present",
    )
    assert result["state"] == "written"


@pytest.mark.skip(reason="superseded by current-operation workflow semantics")
def test_admin_cli_freeze_retains_submission_attribution_as_diagnostic(
    tmp_path, capsys
):
    app = make_admin(tmp_path)
    sid = insert_submission(app.conn, "cli-discard", "ready")

    status = admin_cli.main(["discard", sid], application=app)

    payload = json.loads(capsys.readouterr().out)
    assert status == 3
    assert payload["code"] == "PROTOCOL_INCOMPATIBLE"
    assert payload["submission_id"] == sid
    assert payload["task_gid"] == f"task-{sid}"
    assert payload["state"] == "unsupported_legacy_workflow"
    assert payload["data"]["legacy_state"] == "ready"
    assert payload["allowed_actions"] == []
    row, details = audit_row(app, "dish-admin.discard")
    assert row["submission_id"] == sid
    assert row["task_gid"] == f"task-{sid}"
    assert details["state"] == "ready"


def test_agent_and_admin_command_surfaces_remain_separate():
    recovered = vars(
        admin_cli.build_parser().parse_args(
            [
                "recover",
                "submission",
                "--outcome",
                "applied",
                "--reason",
                "inspected",
            ]
        )
    )
    discarded = vars(
        admin_cli.build_parser().parse_args(
            ["discard", "submission", "--reason", "abandoned"]
        )
    )
    assert recovered["command"] == "recover"
    assert discarded["command"] == "discard"

    with pytest.raises(DishRuleError):
        cli.build_parser().parse_args(
            [
                "recover",
                "submission",
                "--outcome",
                "applied",
                "--reason",
                "inspected",
            ]
        )
    with pytest.raises(DishRuleError):
        admin_cli.build_parser().parse_args(
            ["submit", "submission", "--file", "candidate.md"]
        )
