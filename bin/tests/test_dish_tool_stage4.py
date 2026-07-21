import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dish_tool.admin import DishAdminApplication
from dish_tool import admin_cli, cli
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.models import ResolvedRelease

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "protocol-release"
SECTIONS = [
    {"gid": "research", "name": "Research Queue"},
    {"gid": "verification", "name": "Verification Queue"},
    {"gid": "123456", "name": "Planned"},
    {"gid": "654321", "name": "Finished"},
    {"gid": "sourcing", "name": "Sourcing"},
    {"gid": "reference", "name": "Reference"},
]
PLANNING_NOTE = """# PLANNING BRIEF
Destination section: Planned (123456)
Exemptions: [nutrition-fat] Marco approved lower fat target
"""
COMPLETE_NOTE = """# DISH
Exemptions: [nutrition-fat] Marco approved lower fat target
Destination section: Planned (123456)
Self-verified: yes
Verification: original verification text
## QUANTITIES
Portions: 2
## PROCESS RECORD
"""


def release_fixture():
    planning_text = (FIXTURE_DIR / "dish-planning-manifest.json").read_text()
    complete_text = (FIXTURE_DIR / "dish-complete-task-manifest.json").read_text()
    return ResolvedRelease(
        version="fixture-v1",
        commit="fixture-commit",
        root=FIXTURE_DIR,
        protocols={
            "planning": (FIXTURE_DIR / "dish-planning-protocol.md").read_text(),
            "research": (FIXTURE_DIR / "dish-research-protocol.md").read_text(),
            "verification": (FIXTURE_DIR / "dish-verification-protocol.md").read_text(),
        },
        manifests={
            "planning": json.loads(planning_text),
            "complete_task": json.loads(complete_text),
        },
        manifest_texts={"planning": planning_text, "complete_task": complete_text},
    )


def task(notes, section="research"):
    name = next(s["name"] for s in SECTIONS if s["gid"] == section)
    return {
        "gid": "task",
        "name": "Task",
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
    def __init__(self):
        self.item = task(PLANNING_NOTE)
        self.sections = list(SECTIONS)
        self.moves = []

    def list_sections(self, project_gid):
        return list(self.sections)

    def read_task(self, task_gid):
        return dict(self.item)

    def create_bare_task(self, **kwargs):
        raise AssertionError

    def move_task_to_section(self, *, task_gid, section_gid):
        self.moves.append((task_gid, section_gid))
        name = next(s["name"] for s in self.sections if s["gid"] == section_gid)
        self.item["memberships"][0]["section"] = {"gid": section_gid, "name": name}


def make_app(tmp_path):
    backend = Backend()
    conn = initialize_database(tmp_path / "dish.db")
    app = DishApplication(conn, backend, release_loader=release_fixture)
    return app, backend


def write_candidate(tmp_path, text, name="candidate.md"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def awaiting_verification(app, tmp_path):
    started = app.execute(
        "start", agent="claude", task_gid="task", kind="initial"
    )
    assert started["ok"]
    sid = started["submission_id"]
    prepared = app.execute(
        "prepare",
        agent="claude",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE),
    )
    assert prepared["state"] == "awaiting_verification"
    return sid


def row(app, sid):
    return app.conn.execute(
        "SELECT * FROM submissions WHERE submission_id=?", (sid,)
    ).fetchone()


def audit_details(app, event_type):
    records = app.conn.execute(
        "SELECT details FROM audit_events WHERE event_type=? ORDER BY created_at",
        (event_type,),
    ).fetchall()
    return [json.loads(record["details"]) for record in records]


def test_approve_and_reject_require_opposite_family(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    candidate = write_candidate(tmp_path, COMPLETE_NOTE, "final.md")

    approved = app.execute(
        "approve",
        agent="claude",
        submission_id=sid,
        file_path=candidate,
        correction="none",
    )
    rejected = app.execute(
        "reject", agent="claude", submission_id=sid, reason="not signable"
    )

    assert approved["code"] == "VERIFIER_FAMILY_MISMATCH"
    assert rejected["code"] == "VERIFIER_FAMILY_MISMATCH"
    assert row(app, sid)["status"] == "awaiting_verification"


def test_approve_and_reject_reject_wrong_state(tmp_path):
    app, _ = make_app(tmp_path)
    started = app.execute(
        "start", agent="claude", task_gid="task", kind="initial"
    )
    sid = started["submission_id"]
    candidate = write_candidate(tmp_path, COMPLETE_NOTE, "not-prepared.md")

    approved = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=candidate,
        correction="none",
    )
    rejected = app.execute(
        "reject", agent="gpt", submission_id=sid, reason="not signable"
    )

    assert approved["code"] == "WRONG_STATE"
    assert rejected["code"] == "WRONG_STATE"
    assert row(app, sid)["status"] == "drafting"


def test_approve_revalidates_and_records_verifier(tmp_path):
    app, backend = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)

    result = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "final.md"),
        correction="none",
    )

    assert result["state"] == "ready"
    saved = row(app, sid)
    assert saved["verifier_agent"] == "gpt"
    assert saved["verifier_family"] == "gpt"
    assert saved["approved_at"]
    assert backend.moves == [("task", "verification")]
    details = audit_details(app, "dish.approve")[-1]
    assert details["decision"] == "approve"
    assert details["correction"] == "none"


def test_approve_small_accepts_valid_same_pass_correction(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    corrected = COMPLETE_NOTE.replace("Portions: 2", "Portions: 3")

    result = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, corrected, "corrected.md"),
        correction="small",
    )

    assert result["state"] == "ready"


def test_material_correction_cannot_be_approved_in_place(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)

    result = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "final.md"),
        correction="material",
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert any(error["rule"] == "invalid_correction" for error in result["errors"])
    assert row(app, sid)["status"] == "awaiting_verification"


def test_destination_change_returns_to_drafting_without_counting_rejection(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    changed = COMPLETE_NOTE.replace(
        "Destination section: Planned (123456)",
        "Destination section: Finished (654321)",
    )

    result = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, changed, "changed-destination.md"),
        correction="small",
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["state"] == "drafting"
    assert row(app, sid)["failed_verification_passes"] == 0
    assert any(error["rule"] == "destination_changed_since_prepare" for error in result["errors"])


def test_newly_unresolved_destination_returns_to_drafting(tmp_path):
    app, backend = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    backend.sections = [s for s in backend.sections if s["gid"] != "123456"]

    result = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "final.md"),
        correction="none",
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["state"] == "drafting"
    assert row(app, sid)["failed_verification_passes"] == 0
    assert any(error["rule"] == "destination_unresolved" for error in result["errors"])


def test_exemption_change_is_rejected_without_automatic_state_change(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    changed = COMPLETE_NOTE.replace(
        "Exemptions: [nutrition-fat] Marco approved lower fat target",
        "Exemptions: None",
    )

    result = app.execute(
        "approve",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, changed, "changed-exemptions.md"),
        correction="small",
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["state"] == "awaiting_verification"
    assert any(error["rule"] == "prepared_exemptions_changed" for error in result["errors"])


def test_reject_take_ownership_changes_editor_and_next_route(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)

    rejected = app.execute(
        "reject",
        agent="gpt",
        submission_id=sid,
        reason="material correction required",
        take_ownership=True,
    )
    assert rejected["state"] == "drafting"
    saved = row(app, sid)
    assert saved["editor_agent"] == "gpt"
    assert saved["editor_family"] == "gpt"

    prepared = app.execute(
        "prepare",
        agent="gpt",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "owned.md"),
    )
    assert prepared["state"] == "awaiting_verification"
    assert row(app, sid)["required_verifier_family"] == "claude"


def test_second_rejection_requires_change_and_escalates_with_both_reasons(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)

    first = app.execute(
        "reject", agent="gpt", submission_id=sid, reason="first reason"
    )
    assert first["state"] == "drafting"
    assert row(app, sid)["failed_verification_passes"] == 1

    prepared = app.execute(
        "prepare",
        agent="claude",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "second-pass.md"),
    )
    assert prepared["state"] == "awaiting_verification"

    missing_change = app.execute(
        "reject", agent="gpt", submission_id=sid, reason="second reason"
    )
    assert missing_change["code"] == "INVALID_ARGUMENT"
    assert row(app, sid)["failed_verification_passes"] == 1

    second = app.execute(
        "reject",
        agent="gpt",
        submission_id=sid,
        reason="second reason",
        changed_since_prior="reworked the evidence and method",
    )
    assert second["code"] == "HUMAN_ACTION_REQUIRED"
    assert second["state"] == "awaiting_human"
    assert second["allowed_actions"] == []
    assert row(app, sid)["failed_verification_passes"] == 2

    details = audit_details(app, "dish.reject")[-1]
    assert details["escalation_summary"] == {
        "first_rejection_reason": "first reason",
        "second_rejection_reason": "second reason",
        "changed_since_prior": "reworked the evidence and method",
    }


def test_agent_workflow_commands_are_blocked_in_awaiting_human(tmp_path):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    app.execute("reject", agent="gpt", submission_id=sid, reason="first reason")
    app.execute(
        "prepare",
        agent="claude",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "second-pass.md"),
    )
    app.execute(
        "reject",
        agent="gpt",
        submission_id=sid,
        reason="second reason",
        changed_since_prior="changed evidence",
    )

    calls = [
        app.execute(
            "prepare",
            agent="claude",
            submission_id=sid,
            file_path=write_candidate(tmp_path, COMPLETE_NOTE, "blocked.md"),
        ),
        app.execute(
            "approve",
            agent="gpt",
            submission_id=sid,
            file_path=write_candidate(tmp_path, COMPLETE_NOTE, "blocked-final.md"),
            correction="none",
        ),
        app.execute(
            "reject", agent="gpt", submission_id=sid, reason="blocked"
        ),
    ]

    assert [result["code"] for result in calls] == [
        "HUMAN_ACTION_REQUIRED",
        "HUMAN_ACTION_REQUIRED",
        "HUMAN_ACTION_REQUIRED",
    ]
    assert all(result["allowed_actions"] == [] for result in calls)


def test_admin_unblock_requires_state_and_reason_and_retains_history(tmp_path):
    app, _ = make_app(tmp_path)
    admin = DishAdminApplication(app.conn)
    sid = awaiting_verification(app, tmp_path)

    wrong_state = admin.execute("unblock", submission_id=sid, reason="changed evidence")
    assert wrong_state["code"] == "WRONG_STATE"

    app.execute("reject", agent="gpt", submission_id=sid, reason="first reason")
    app.execute(
        "prepare",
        agent="claude",
        submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE, "second-pass.md"),
    )
    app.execute(
        "reject",
        agent="gpt",
        submission_id=sid,
        reason="second reason",
        changed_since_prior="changed evidence",
    )
    before = app.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    missing_reason = admin.execute("unblock", submission_id=sid, reason="  ")
    assert missing_reason["code"] == "INVALID_ARGUMENT"
    assert row(app, sid)["status"] == "awaiting_human"

    unblocked = admin.execute(
        "unblock", submission_id=sid, reason="new premise and narrower scope"
    )
    assert unblocked["state"] == "drafting"
    assert row(app, sid)["failed_verification_passes"] == 0
    after = app.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert after > before
    details = audit_details(app, "dish-admin.unblock")[-1]
    assert details["reason"] == "new premise and narrower scope"


def test_cli_argument_failures_audit_once_on_each_surface(tmp_path, capsys):
    app, _ = make_app(tmp_path)
    sid = awaiting_verification(app, tmp_path)
    candidate = write_candidate(tmp_path, COMPLETE_NOTE, "cli-final.md")
    before_dish = app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='dish.approve'"
    ).fetchone()[0]

    status = cli.main(
        ["approve", sid, "--agent", "gpt", "--file", candidate],
        application=app,
    )
    payload = json.loads(capsys.readouterr().out)
    after_dish = app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='dish.approve'"
    ).fetchone()[0]

    assert status == 2
    assert payload["code"] == "INVALID_ARGUMENT"
    assert after_dish == before_dish + 1

    admin = DishAdminApplication(app.conn)
    before_admin = app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='dish-admin.unblock'"
    ).fetchone()[0]
    status = admin_cli.main(["unblock", sid], application=admin)
    payload = json.loads(capsys.readouterr().out)
    after_admin = app.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='dish-admin.unblock'"
    ).fetchone()[0]

    assert status == 2
    assert payload["code"] == "INVALID_ARGUMENT"
    assert after_admin == before_admin + 1


def test_concurrent_approve_and_reject_allow_one_transition(tmp_path):
    db_path = tmp_path / "dish.db"
    backend = Backend()
    setup = DishApplication(
        initialize_database(db_path), backend, release_loader=release_fixture
    )
    sid = awaiting_verification(setup, tmp_path)
    setup.conn.close()
    candidate = write_candidate(tmp_path, COMPLETE_NOTE, "race-final.md")
    barrier = threading.Barrier(2)

    def run(command):
        conn = initialize_database(db_path)
        app = DishApplication(conn, backend, release_loader=release_fixture)
        barrier.wait()
        try:
            if command == "approve":
                return app.execute(
                    "approve",
                    agent="gpt",
                    submission_id=sid,
                    file_path=candidate,
                    correction="none",
                )
            return app.execute(
                "reject", agent="gpt", submission_id=sid, reason="race rejection"
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["approve", "reject"]))

    assert sum(result["ok"] for result in results) == 1
    final_conn = initialize_database(db_path)
    final = final_conn.execute(
        "SELECT status FROM submissions WHERE submission_id=?", (sid,)
    ).fetchone()["status"]
    final_conn.close()
    assert final in {"ready", "drafting"}
