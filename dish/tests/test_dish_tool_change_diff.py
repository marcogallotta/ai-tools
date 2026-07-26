"""Change-diff telemetry for successful change preparation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN_DIR))

from dish_tool.commands import DishApplication  # noqa: E402
from dish_tool.database import initialize_database  # noqa: E402
from dish_tool.models import ResolvedRelease  # noqa: E402
from dish_tool.telemetry import calculate_change_diff  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "protocol-release"
SECTIONS = [
    {"gid": "research", "name": "Research Queue"},
    {"gid": "verification", "name": "Verification Queue"},
    {"gid": "123456", "name": "Planned"},
    {"gid": "sourcing", "name": "Sourcing"},
    {"gid": "reference", "name": "Reference"},
]
PLANNING_NOTE = """# PLANNING BRIEF
Destination section: Planned (123456)
Exemptions: None
"""
CANONICAL_TITLE = "Dish — recognition"
TITLE_ARGS = {
    "dish_name": "Dish",
    "recognition": "recognition",
    "no_role_tags": True,
    "no_blockers": True,
}
COMPLETE_NOTE = """# DISH
Exemptions: None
Destination section: Planned (123456)
Self-verified: yes
Verification: original verification text
## QUANTITIES
Portions: 2
## PROCESS RECORD
"""


def release_fixture() -> ResolvedRelease:
    planning_text = (FIXTURE_DIR / "dish-planning-manifest.json").read_text()
    complete_text = (
        FIXTURE_DIR / "dish-complete-task-manifest.json"
    ).read_text()
    return ResolvedRelease(
        version="fixture-v2-structured-title",
        commit="fixture-commit",
        root=FIXTURE_DIR,
        protocols={
            "planning": (FIXTURE_DIR / "dish-planning-protocol.md").read_text(),
            "research": (FIXTURE_DIR / "dish-research-protocol.md").read_text(),
            "verification": (
                FIXTURE_DIR / "dish-verification-protocol.md"
            ).read_text(),
        },
        manifests={
            "planning": json.loads(planning_text),
            "complete_task": json.loads(complete_text),
        },
        manifest_texts={
            "planning": planning_text,
            "complete_task": complete_text,
        },
    )


def task(notes: str, section: str = "research") -> dict:
    name = next(item["name"] for item in SECTIONS if item["gid"] == section)
    return {
        "gid": "task",
        "name": CANONICAL_TITLE,
        "notes": notes,
        "projects": [{"gid": "1215089183018968"}],
        "memberships": [
            {
                "project": {"gid": "1215089183018968"},
                "section": {"gid": section, "name": name},
            }
        ],
    }


class Backend:
    def __init__(self, item: dict) -> None:
        self.item = item
        self.moves: list[tuple[str, str]] = []
        self.fail_move = False
        self.read_calls = 0
        self.fail_read_calls: set[int] = set()

    def list_sections(self, project_gid: str) -> list[dict]:
        return list(SECTIONS)

    def read_task(self, task_gid: str) -> dict:
        self.read_calls += 1
        if self.read_calls in self.fail_read_calls:
            raise RuntimeError("read failed")
        return dict(self.item)

    def create_bare_task(self, **kwargs):
        raise AssertionError

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None:
        self.moves.append((task_gid, section_gid))
        if self.fail_move:
            raise RuntimeError("move failed")
        self.item["memberships"][0]["section"] = {
            "gid": section_gid,
            "name": "Verification Queue",
        }


def make_app(tmp_path, notes: str):
    backend = Backend(task(notes))
    app = DishApplication(
        initialize_database(tmp_path / "dish.db"),
        backend,
        release_loader=release_fixture,
    )
    return app, backend


def write_candidate(tmp_path, text: str) -> str:
    path = tmp_path / "candidate.md"
    path.write_text(text)
    return str(path)


def start_change(app, *, level: str = "small") -> str:
    result = app.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind="change",
        change_level=level,
        change_reason="test change",
    )
    assert result["ok"]
    return result["submission_id"]


def latest_prepare_details(app, submission_id: str) -> dict:
    row = app.conn.execute(
        """
        SELECT details
          FROM audit_events
         WHERE submission_id = ? AND event_type = 'dish.prepare'
         ORDER BY created_at DESC, rowid DESC
         LIMIT 1
        """,
        (submission_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["details"])


def test_calculate_change_diff_counts_and_heading_context() -> None:
    manifest = {
        "headings": {
            "allowed": ["# DISH", "## QUANTITIES", "## PROCESS RECORD"]
        }
    }
    summary = calculate_change_diff(
        "# DISH\nAAA\n## PROCESS RECORD\nsame\n",
        "# DISH\nBBBB\n## PROCESS RECORD\nsame\n",
        manifest,
    )

    assert summary == {
        "characters_added": 4,
        "characters_removed": 3,
        "lines_added": 1,
        "lines_removed": 1,
        "headings_changed": ["# DISH"],
    }


def test_calculate_change_diff_orders_all_changed_canonical_headings() -> None:
    manifest = {
        "headings": {
            "allowed": ["# DISH", "## QUANTITIES", "## PROCESS RECORD"]
        }
    }
    summary = calculate_change_diff(
        "# DISH\nintro\n## QUANTITIES\nPortions: 2\n"
        "## PROCESS RECORD\nVerification: x\n",
        "# DISH\nintro changed\n## QUANTITIES\nPortions: 2\nSalt: 1 g\n"
        "## PROCESS RECORD\nVerification: x\n",
        manifest,
    )

    assert summary["headings_changed"] == ["# DISH", "## QUANTITIES"]
    assert summary["lines_added"] == 2
    assert summary["lines_removed"] == 1


def test_successful_change_prepare_audits_diff_against_current_live_notes(
    tmp_path,
) -> None:
    app, backend = make_app(tmp_path, COMPLETE_NOTE)
    submission_id = start_change(app)
    live_notes = COMPLETE_NOTE.replace(
        "Exemptions: None", "live-only line\nExemptions: None"
    )
    candidate = COMPLETE_NOTE.replace(
        "Exemptions: None", "candidate-only line\nExemptions: None"
    )
    backend.item["notes"] = live_notes

    result = app.execute(
        "prepare",
        agent="claude",
        submission_id=submission_id,
        file_path=write_candidate(tmp_path, candidate),
        **TITLE_ARGS,
    )

    assert result["state"] == "ready"
    details = latest_prepare_details(app, submission_id)
    expected = calculate_change_diff(
        live_notes, candidate, release_fixture().manifests["complete_task"]
    )
    assert details["change_diff"] == expected
    assert "live-only line" not in json.dumps(details)
    assert "candidate-only line" not in json.dumps(details)


def test_non_change_and_failed_prepare_events_have_no_diff_summary(tmp_path) -> None:
    app, _ = make_app(tmp_path, PLANNING_NOTE)
    initial = app.execute(
        "start", agent="claude", task_gid="task", kind="initial"
    )
    initial_id = initial["submission_id"]
    passed = app.execute(
        "prepare",
        agent="claude",
        submission_id=initial_id,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE),
        **TITLE_ARGS,
    )
    assert passed["ok"]
    assert "change_diff" not in latest_prepare_details(app, initial_id)

    app2, _ = make_app(tmp_path / "failed", COMPLETE_NOTE)
    change_id = start_change(app2)
    failed = app2.execute(
        "prepare",
        agent="claude",
        submission_id=change_id,
        file_path=write_candidate(tmp_path / "failed", "invalid"),
        **TITLE_ARGS,
    )
    assert failed["code"] == "VALIDATION_FAILED"
    assert "change_diff" not in latest_prepare_details(app2, change_id)


def test_move_only_retry_copies_diff_to_successful_prepare_audit(tmp_path) -> None:
    app, backend = make_app(tmp_path, COMPLETE_NOTE)
    submission_id = start_change(app, level="large")
    candidate = COMPLETE_NOTE.replace(
        "Exemptions: None", "material change\nExemptions: None"
    )
    backend.fail_move = True

    first = app.execute(
        "prepare",
        agent="claude",
        submission_id=submission_id,
        file_path=write_candidate(tmp_path, candidate),
        **TITLE_ARGS,
    )
    assert first["code"] == "INTERNAL_ERROR"
    first_diff = latest_prepare_details(app, submission_id)["change_diff"]

    backend.fail_move = False
    second = app.execute(
        "prepare",
        agent="claude",
        submission_id=submission_id,
        file_path="not-read-on-retry",
    )

    assert second["state"] == "awaiting_verification"
    details = latest_prepare_details(app, submission_id)
    assert details["ok"] is True
    assert details["change_diff"] == first_diff


def test_telemetry_read_failure_never_blocks_change_prepare(tmp_path) -> None:
    app, backend = make_app(tmp_path, COMPLETE_NOTE)
    submission_id = start_change(app)
    backend.fail_read_calls.add(2)

    result = app.execute(
        "prepare",
        agent="claude",
        submission_id=submission_id,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE),
        **TITLE_ARGS,
    )

    assert result["state"] == "ready"
    details = latest_prepare_details(app, submission_id)
    assert details["change_diff_unavailable"] == "live_task_read_failed"
    assert "change_diff" not in details


def test_move_only_retry_copies_unavailable_reason_to_success(tmp_path) -> None:
    app, backend = make_app(tmp_path, COMPLETE_NOTE)
    submission_id = start_change(app, level="large")
    backend.fail_read_calls.add(2)
    backend.fail_move = True

    first = app.execute(
        "prepare",
        agent="claude",
        submission_id=submission_id,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE),
        **TITLE_ARGS,
    )
    assert first["code"] == "INTERNAL_ERROR"
    assert (
        latest_prepare_details(app, submission_id)["change_diff_unavailable"]
        == "live_task_read_failed"
    )

    backend.fail_move = False
    second = app.execute(
        "prepare",
        agent="claude",
        submission_id=submission_id,
        file_path="not-read-on-retry",
    )

    assert second["state"] == "awaiting_verification"
    assert (
        latest_prepare_details(app, submission_id)["change_diff_unavailable"]
        == "live_task_read_failed"
    )
