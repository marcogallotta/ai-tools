import json
from pathlib import Path

from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.models import ResolvedRelease

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
CANONICAL_TITLE = "Dish — recognition"
TITLE_ARGS = {
    "dish_name": "Dish",
    "recognition": "recognition",
    "no_role_tags": True,
    "no_blockers": True,
}


def release_fixture():
    planning_text = (FIXTURE_DIR / "dish-planning-manifest.json").read_text()
    complete_text = (FIXTURE_DIR / "dish-complete-task-manifest.json").read_text()
    return ResolvedRelease(
        version="fixture-v2-structured-title", commit="fixture-commit", root=FIXTURE_DIR,
        protocols={
            "planning": (FIXTURE_DIR / "dish-planning-protocol.md").read_text(),
            "research": (FIXTURE_DIR / "dish-research-protocol.md").read_text(),
            "verification": (FIXTURE_DIR / "dish-verification-protocol.md").read_text(),
        },
        manifests={"planning": json.loads(planning_text), "complete_task": json.loads(complete_text)},
        manifest_texts={"planning": planning_text, "complete_task": complete_text},
    )


def task(notes, section="research"):
    name = next(s["name"] for s in SECTIONS if s["gid"] == section)
    return {
        "gid": "task", "name": CANONICAL_TITLE, "notes": notes,
        "projects": [{"gid": "1215089183018968"}],
        "memberships": [{"project": {"gid": "1215089183018968"}, "section": {"gid": section, "name": name}}],
    }


class Backend:
    def __init__(self, item):
        self.item = item
        self.moves = []
        self.fail_move = False
    def list_sections(self, project_gid):
        return list(SECTIONS)
    def read_task(self, task_gid):
        return dict(self.item)
    def create_bare_task(self, **kwargs):
        raise AssertionError
    def move_task_to_section(self, *, task_gid, section_gid):
        self.moves.append((task_gid, section_gid))
        if self.fail_move:
            raise RuntimeError("move failed")
        self.item["memberships"][0]["section"] = {"gid": section_gid, "name": "Verification Queue"}


def make_app(tmp_path, notes, section="research"):
    backend = Backend(task(notes, section))
    return DishApplication(initialize_database(tmp_path / "dish.db"), backend, release_loader=release_fixture), backend


def write_candidate(tmp_path, text):
    path = tmp_path / "candidate.md"
    path.write_text(text)
    return str(path)


def start(application, kind, **kwargs):
    result = application.execute("start", agent="claude", task_gid="task", kind=kind, **kwargs)
    assert result["ok"]
    return result["submission_id"]


def test_planning_prepare_goes_ready_without_move(tmp_path):
    app, backend = make_app(tmp_path, "")
    sid = start(app, "planning")
    result = app.execute("prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid, file_path=write_candidate(tmp_path, PLANNING_NOTE))
    assert result["state"] == "ready"
    assert backend.moves == []


def test_initial_prepare_routes_to_opposite_family_and_moves(tmp_path):
    app, backend = make_app(tmp_path, PLANNING_NOTE)
    sid = start(app, "initial")
    result = app.execute(
        "prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE), **TITLE_ARGS
    )
    assert result["state"] == "awaiting_verification"
    assert backend.moves == [("task", "verification")]
    row = app.conn.execute("SELECT * FROM submissions WHERE submission_id=?", (sid,)).fetchone()
    assert row["required_verifier_family"] == "gpt"
    assert row["destination_section_gid"] == "123456"
    assert row["research_queue_moved_at"]


def test_prepare_reports_multiple_validation_failures_and_stays_drafting(tmp_path):
    app, _ = make_app(tmp_path, PLANNING_NOTE)
    sid = start(app, "initial")
    result = app.execute(
        "prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid,
        file_path=write_candidate(tmp_path, "bad"), **TITLE_ARGS
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert result["state"] == "drafting"
    assert len(result["errors"]) >= 4


def test_small_change_preserves_verification_and_exemptions(tmp_path):
    app, backend = make_app(tmp_path, COMPLETE_NOTE)
    sid = start(app, "change", change_level="small", change_reason="typo")
    changed = COMPLETE_NOTE.replace("Verification: original verification text", "Verification: changed")
    result = app.execute(
        "prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid,
        file_path=write_candidate(tmp_path, changed), **TITLE_ARGS
    )
    assert result["code"] == "VALIDATION_FAILED"
    assert any(e["rule"] == "verification_line_changed" for e in result["errors"])
    assert backend.moves == []


def test_large_exemption_change_requires_revision(tmp_path):
    app, _ = make_app(tmp_path, COMPLETE_NOTE)
    sid = start(app, "change", change_level="large", change_reason="new nutrition decision")
    changed = COMPLETE_NOTE.replace("[nutrition-fat] Marco approved lower fat target", "None")
    path = write_candidate(tmp_path, changed)
    failed = app.execute(
        "prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid,
        file_path=path, **TITLE_ARGS
    )
    assert any(e["rule"] == "exemption_revision_required" for e in failed["errors"])
    passed = app.execute(
        "prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid, file_path=path,
        exemption_revision="Marco, 2026-07-21: removed exemption", **TITLE_ARGS
    )
    assert passed["state"] == "awaiting_verification"


def test_research_handoff_retry_skips_validation_and_only_moves(tmp_path):
    app, backend = make_app(tmp_path, PLANNING_NOTE)
    sid = start(app, "initial")
    backend.fail_move = True
    first = app.execute(
        "prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid,
        file_path=write_candidate(tmp_path, COMPLETE_NOTE), **TITLE_ARGS
    )
    assert first["code"] == "INTERNAL_ERROR"
    row = app.conn.execute("SELECT status FROM submissions WHERE submission_id=?", (sid,)).fetchone()
    assert row["status"] == "research_handoff"
    backend.fail_move = False
    second = app.execute("prepare", model="gpt-5.6-sol", agent="claude", submission_id=sid, file_path="missing-does-not-matter")
    assert second["state"] == "awaiting_verification"
    assert len(backend.moves) == 2
