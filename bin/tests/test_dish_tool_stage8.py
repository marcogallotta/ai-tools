"""Stage 8: read-only operational reporting queries."""

from __future__ import annotations

import re
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN_DIR))

from dish_tool.database import initialize_database, record_audit  # noqa: E402

REPORTS_PATH = BIN_DIR / "dish-reports.sql"
REPORT_PATTERN = re.compile(
    r"^-- report: (?P<name>[a-z_]+)\n(?P<sql>.*?)^-- end report$",
    re.MULTILINE | re.DOTALL,
)
EXPECTED_REPORTS = {
    "command_counts",
    "validation_failure_rates",
    "rejection_rates",
    "human_review_rates",
    "submit_outcomes",
    "change_diff_distributions",
    "advisory_bypasses",
}


def _reports() -> dict[str, str]:
    text = REPORTS_PATH.read_text(encoding="utf-8")
    reports = {
        match.group("name"): match.group("sql").strip()
        for match in REPORT_PATTERN.finditer(text)
    }
    assert set(reports) == EXPECTED_REPORTS
    return reports


def _insert_submission(
    conn,
    *,
    submission_id: str,
    task_gid: str,
    kind: str,
    change_level: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, change_level, status, created_at
        ) VALUES (?, ?, ?, 'fixture-v1', 'fixture-commit', '{}', '{}',
                  'claude', 'claude', ?, 'consumed', '2026-07-01T00:00:00+00:00')
        """,
        (submission_id, task_gid, kind, change_level),
    )


def _audit(
    conn,
    *,
    event_type: str,
    details: dict,
    submission_id: str | None = None,
    task_gid: str | None = None,
    actor_agent: str | None = None,
    second: int,
) -> None:
    record_audit(
        conn,
        submission_id=submission_id,
        task_gid=task_gid,
        event_type=event_type,
        actor_agent=actor_agent,
        details=details,
        created_at=f"2026-07-01T00:00:{second:02d}+00:00",
    )


def _command_details(
    command: str,
    *,
    ok: bool,
    code: str,
    state: str | None,
    errors: list[dict] | None = None,
    **extra,
) -> dict:
    return {
        "command": command,
        "ok": ok,
        "code": code,
        "state": state,
        "retryable": not ok,
        "errors": list(errors or []),
        **extra,
    }


def _fixture_connection(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _insert_submission(
        conn,
        submission_id="change-small",
        task_gid="task-a",
        kind="change",
        change_level="small",
    )
    _insert_submission(
        conn,
        submission_id="initial",
        task_gid="task-b",
        kind="initial",
    )
    _insert_submission(
        conn,
        submission_id="planning",
        task_gid="task-c",
        kind="planning",
    )
    _insert_submission(
        conn,
        submission_id="change-large",
        task_gid="task-d",
        kind="change",
        change_level="large",
    )

    second = 0

    def add(**kwargs):
        nonlocal second
        second += 1
        _audit(conn, second=second, **kwargs)

    add(
        event_type="dish.inspect",
        submission_id="change-small",
        task_gid="task-a",
        actor_agent="claude",
        details=_command_details("inspect", ok=True, code="OK", state="ready"),
    )
    add(
        event_type="dish.start",
        submission_id="change-small",
        task_gid="task-a",
        actor_agent="claude",
        details=_command_details(
            "start",
            ok=True,
            code="OK",
            state="drafting",
            submission_kind="change",
            change_level="small",
        ),
    )
    add(
        event_type="dish.start",
        task_gid="task-z",
        actor_agent="gpt",
        details=_command_details(
            "start",
            ok=False,
            code="VALIDATION_FAILED",
            state=None,
            errors=[
                {"rule": "heading_missing", "heading": "WHAT TO BUY"},
                {"rule": "heading_missing", "heading": "CHECK BEFORE COOKING"},
                {"rule": "invalid_exemptions"},
            ],
            submission_kind="change",
            change_level="large",
        ),
    )
    add(
        event_type="dish.prepare",
        submission_id="change-small",
        task_gid="task-a",
        actor_agent="claude",
        details=_command_details(
            "prepare",
            ok=False,
            code="VALIDATION_FAILED",
            state="drafting",
            errors=[
                {"rule": "heading_missing"},
                {"rule": "invalid_exemptions"},
            ],
        ),
    )
    add(
        event_type="dish.prepare",
        submission_id="initial",
        task_gid="task-b",
        actor_agent="gpt",
        details=_command_details(
            "prepare", ok=True, code="OK", state="awaiting_verification"
        ),
    )
    add(
        event_type="dish.approve",
        submission_id="initial",
        task_gid="task-b",
        actor_agent="claude",
        details=_command_details(
            "approve",
            ok=True,
            code="OK",
            state="ready",
            decision="approve",
        ),
    )
    add(
        event_type="dish.reject",
        submission_id="change-small",
        task_gid="task-a",
        actor_agent="gpt",
        details=_command_details(
            "reject",
            ok=True,
            code="OK",
            state="drafting",
            decision="reject",
            failed_verification_passes=1,
        ),
    )
    add(
        event_type="dish.reject",
        submission_id="change-small",
        task_gid="task-a",
        actor_agent="gpt",
        details=_command_details(
            "reject",
            ok=False,
            code="HUMAN_ACTION_REQUIRED",
            state="awaiting_human",
            errors=[{"rule": "human_review_required"}],
            decision="reject",
            failed_verification_passes=2,
        ),
    )
    add(
        event_type="dish.reject",
        submission_id="change-large",
        task_gid="task-d",
        actor_agent="claude",
        details=_command_details(
            "reject",
            ok=False,
            code="CONFLICT",
            state="awaiting_verification",
            errors=[{"rule": "concurrent_state_change"}],
            decision="reject",
            failed_verification_passes=1,
        ),
    )
    add(
        event_type="dish-admin.unblock",
        submission_id="change-small",
        task_gid="task-a",
        details=_command_details(
            "unblock", ok=True, code="OK", state="drafting", actor_role="marco"
        ),
    )

    submit_cases = [
        (
            "change-small",
            "task-a",
            "claude",
            True,
            "OK",
            "consumed",
            "confirmed_success",
        ),
        (
            "initial",
            "task-b",
            "gpt",
            False,
            "BACKEND_REJECTED",
            "written",
            "confirmed_success",
        ),
        (
            "planning",
            "task-c",
            "claude",
            False,
            "BACKEND_UNCERTAIN",
            "uncertain",
            "uncertain",
        ),
        (
            "change-large",
            "task-d",
            "codex",
            False,
            "BACKEND_REJECTED",
            "ready",
            "confirmed_non_application",
        ),
    ]
    for submission_id, task_gid, actor, ok, code, state, write_outcome in submit_cases:
        add(
            event_type="dish.submit",
            submission_id=submission_id,
            task_gid=task_gid,
            actor_agent=actor,
            details=_command_details(
                "submit",
                ok=ok,
                code=code,
                state=state,
                write_outcome=write_outcome,
            ),
        )
    add(
        event_type="dish.submit",
        submission_id="planning",
        task_gid="task-c",
        actor_agent="claude",
        details=_command_details(
            "submit",
            ok=False,
            code="WRONG_STATE",
            state="consumed",
            errors=[{"rule": "wrong_state"}],
        ),
    )

    for _ in range(2):
        add(
            event_type="generic_note_bypass",
            task_gid="task-a",
            actor_agent="codex",
            details={
                "command": "set-notes",
                "mode": "v1a_advisory",
                "resolution": "managed_section",
                "fields": ["notes"],
            },
        )
    add(
        event_type="generic_note_bypass",
        task_gid=None,
        actor_agent=None,
        details={
            "command": "create-task",
            "mode": "v1a_advisory",
            "resolution": "section_unresolved",
            "fields": ["notes"],
        },
    )
    return conn


def _rows(conn, report_name: str):
    return conn.execute(_reports()[report_name]).fetchall()


def _row_by(rows, **expected):
    for row in rows:
        if all(row[key] == value for key, value in expected.items()):
            return row
    raise AssertionError(f"row not found: {expected}; rows={list(map(dict, rows))}")


def test_report_file_contains_exact_named_read_only_queries(tmp_path):
    conn = initialize_database(tmp_path / "empty.db")
    conn.execute("PRAGMA query_only = ON")

    results = {}
    for name, sql in _reports().items():
        rows = conn.execute(sql).fetchall()
        assert isinstance(rows, list), name
        results[name] = rows

    rejection = results["rejection_rates"][0]
    assert rejection["verifier_decisions"] == 0
    assert rejection["approvals"] == 0
    assert rejection["rejections"] == 0
    assert rejection["tasks_with_repeated_rejection"] == 0


def test_command_counts_include_inspect_and_resolve_kind_and_level(tmp_path):
    conn = _fixture_connection(tmp_path)
    rows = _rows(conn, "command_counts")

    inspect = _row_by(
        rows,
        command="inspect",
        actor_agent="claude",
        submission_kind="change",
        change_level="small",
    )
    assert dict(inspect) == {
        "command": "inspect",
        "actor_agent": "claude",
        "submission_kind": "change",
        "change_level": "small",
        "command_count": 1,
        "successful_count": 1,
        "failed_count": 0,
    }
    failed_start = _row_by(
        rows,
        command="start",
        actor_agent="gpt",
        submission_kind="change",
        change_level="large",
    )
    assert failed_start["command_count"] == 1
    assert failed_start["successful_count"] == 0
    assert failed_start["failed_count"] == 1


def test_validation_failure_rates_deduplicate_rules_per_invocation(tmp_path):
    conn = _fixture_connection(tmp_path)
    rows = _rows(conn, "validation_failure_rates")

    start_heading = _row_by(rows, command="start", rule="heading_missing")
    assert start_heading["validation_failure_events"] == 1
    assert start_heading["command_events"] == 2
    assert start_heading["validation_failure_rate"] == 0.5

    prepare_exemptions = _row_by(
        rows, command="prepare", rule="invalid_exemptions"
    )
    assert prepare_exemptions["validation_failure_events"] == 1
    assert prepare_exemptions["command_events"] == 2
    assert prepare_exemptions["validation_failure_rate"] == 0.5


def test_rejection_and_repeated_rejection_rates_use_applied_decisions(tmp_path):
    conn = _fixture_connection(tmp_path)
    row = _rows(conn, "rejection_rates")[0]

    assert dict(row) == {
        "verifier_decisions": 3,
        "approvals": 1,
        "rejections": 2,
        "rejection_rate": 0.6667,
        "tasks_with_rejection": 1,
        "tasks_with_repeated_rejection": 1,
        "repeated_rejection_task_rate": 1.0,
    }


def test_human_review_rates_have_explicit_denominators(tmp_path):
    conn = _fixture_connection(tmp_path)
    row = _rows(conn, "human_review_rates")[0]

    assert dict(row) == {
        "successful_rejections": 2,
        "human_escalations": 1,
        "human_escalation_rate_per_rejection": 0.5,
        "successful_unblocks": 1,
        "unblock_rate_per_escalation": 1.0,
        "tasks_escalated": 1,
        "tasks_unblocked": 1,
    }


def test_submit_outcomes_include_attempted_and_not_attempted_calls(tmp_path):
    conn = _fixture_connection(tmp_path)
    rows = _rows(conn, "submit_outcomes")

    consumed = _row_by(
        rows,
        final_state="consumed",
        write_outcome="confirmed_success",
        code="OK",
        ok=1,
    )
    assert consumed["outcome_count"] == 1
    assert consumed["outcome_rate"] == 0.2

    not_attempted = _row_by(
        rows,
        final_state="consumed",
        write_outcome="not_attempted",
        code="WRONG_STATE",
        ok=0,
    )
    assert not_attempted["outcome_count"] == 1
    assert not_attempted["outcome_rate"] == 0.2


def test_advisory_bypasses_group_by_task_agent_command_and_resolution(tmp_path):
    conn = _fixture_connection(tmp_path)
    rows = _rows(conn, "advisory_bypasses")

    managed = _row_by(
        rows,
        task_gid="task-a",
        actor_agent="codex",
        command="set-notes",
        resolution="managed_section",
    )
    assert managed["bypass_count"] == 2
    assert managed["first_seen_at"] < managed["last_seen_at"]

    unresolved_create = _row_by(
        rows,
        task_gid="<pending-create>",
        actor_agent="<unknown>",
        command="create-task",
        resolution="section_unresolved",
    )
    assert unresolved_create["bypass_count"] == 1


def test_change_diff_distributions_group_by_declared_level(tmp_path):
    conn = initialize_database(tmp_path / "dish.db")
    _insert_submission(
        conn,
        submission_id="small",
        task_gid="task-small",
        kind="change",
        change_level="small",
    )
    _insert_submission(
        conn,
        submission_id="large",
        task_gid="task-large",
        kind="change",
        change_level="large",
    )

    small_diff = {
        "characters_added": 4,
        "characters_removed": 3,
        "lines_added": 1,
        "lines_removed": 1,
        "headings_changed": ["# DISH"],
    }
    large_diff = {
        "characters_added": 20,
        "characters_removed": 5,
        "lines_added": 3,
        "lines_removed": 2,
        "headings_changed": ["# DISH", "## QUANTITIES"],
    }
    _audit(
        conn,
        event_type="dish.prepare",
        submission_id="small",
        task_gid="task-small",
        actor_agent="claude",
        details=_command_details(
            "prepare",
            ok=True,
            code="OK",
            state="ready",
            change_diff=small_diff,
        ),
        second=1,
    )
    _audit(
        conn,
        event_type="dish.prepare",
        submission_id="small",
        task_gid="task-small",
        actor_agent="claude",
        details=_command_details(
            "prepare",
            ok=True,
            code="OK",
            state="ready",
            change_diff_unavailable="live_task_read_failed",
        ),
        second=2,
    )
    for second in (3, 4):
        _audit(
            conn,
            event_type="dish.prepare",
            submission_id="large",
            task_gid="task-large",
            actor_agent="gpt",
            details=_command_details(
                "prepare",
                ok=True,
                code="OK",
                state="awaiting_verification",
                change_diff=large_diff,
            ),
            second=second,
        )
    _audit(
        conn,
        event_type="dish.prepare",
        submission_id="large",
        task_gid="task-large",
        actor_agent="gpt",
        details=_command_details(
            "prepare",
            ok=False,
            code="VALIDATION_FAILED",
            state="drafting",
            change_diff=large_diff,
        ),
        second=5,
    )

    rows = _rows(conn, "change_diff_distributions")

    assert _row_by(
        rows,
        change_level="small",
        metric="telemetry_status",
        metric_value="available",
    )["event_count"] == 1
    assert _row_by(
        rows,
        change_level="small",
        metric="telemetry_status",
        metric_value="unavailable",
    )["event_count"] == 1
    assert _row_by(
        rows,
        change_level="small",
        metric="telemetry_unavailable_reason",
        metric_value="live_task_read_failed",
    )["event_count"] == 1
    assert _row_by(
        rows,
        change_level="small",
        metric="characters_added",
        metric_value="4",
    )["event_count"] == 1
    assert _row_by(
        rows,
        change_level="large",
        metric="characters_added",
        metric_value="20",
    )["event_count"] == 2
    assert _row_by(
        rows,
        change_level="large",
        metric="heading_changed",
        metric_value="## QUANTITIES",
    )["event_count"] == 2
    assert _row_by(
        rows,
        change_level="large",
        metric="headings_changed_count",
        metric_value="2",
    )["event_count"] == 2
