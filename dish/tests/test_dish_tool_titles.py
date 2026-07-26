"""Structured-title grammar and guarded workflow ownership."""

from __future__ import annotations

import copy
import json
import sqlite3

import asana
from pathlib import Path

from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.database import MIGRATIONS, initialize_database
from dish_tool.models import ResolvedRelease, TitleFields
from dish_tool.validation import (
    parse_canonical_title,
    render_title,
    validate_manifest_shape,
    validate_title_declaration,
)

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
    complete_text = (FIXTURE_DIR / "dish-complete-task-manifest.json").read_text()
    return ResolvedRelease(
        version="fixture-v2-structured-title",
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


def complete_manifest() -> dict:
    return dict(release_fixture().manifests["complete_task"])


class Backend:
    def __init__(self, *, title: str = "Working title", notes: str = PLANNING_NOTE):
        self.task = {
            "gid": "task",
            "name": title,
            "notes": notes,
            "projects": [{"gid": "1215089183018968"}],
            "memberships": [
                {
                    "project": {"gid": "1215089183018968"},
                    "section": {"gid": "research", "name": "Research Queue"},
                }
            ],
        }
        self.content_writes: list[tuple[str, str, str]] = []

    def list_sections(self, project_gid: str) -> list[dict]:
        return copy.deepcopy(SECTIONS)

    def read_task(self, task_gid: str) -> dict:
        return copy.deepcopy(self.task)

    def create_bare_task(self, **kwargs):
        raise AssertionError

    def update_task_content(self, *, task_gid: str, title: str, notes: str) -> None:
        self.content_writes.append((task_gid, title, notes))
        self.task["name"] = title
        self.task["notes"] = notes

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None:
        name = next(item["name"] for item in SECTIONS if item["gid"] == section_gid)
        self.task["memberships"][0]["section"] = {"gid": section_gid, "name": name}


def make_app(tmp_path, backend: Backend) -> DishApplication:
    return DishApplication(
        initialize_database(tmp_path / "dish.db"),
        backend,
        release_loader=release_fixture,
    )


def write_candidate(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def full_title_args(**overrides):
    values = {
        "dish_name": "Mapo tofu",
        "recognition": "silken tofu in chile-bean sauce",
        "roles": ["comparison", "side"],
        "no_role_tags": False,
        "blockers": ["buy tofu", "check doubanjiang"],
        "no_blockers": False,
    }
    values.update(overrides)
    return values


def test_title_declaration_renders_manifest_role_order_and_declared_blocker_order():
    result = validate_title_declaration(complete_manifest(), **full_title_args())

    assert result.ok
    assert result.title == (
        "[side] [comparison] [buy tofu] [check doubanjiang] "
        "Mapo tofu — silken tofu in chile-bean sauce"
    )
    assert result.fields == TitleFields(
        role_tags=("side", "comparison"),
        blockers=("buy tofu", "check doubanjiang"),
        dish_name="Mapo tofu",
        recognition="silken tofu in chile-bean sauce",
    )
    assert render_title(result.fields, complete_manifest()) == result.title
    assert parse_canonical_title(result.title, complete_manifest()) == result


def test_title_declaration_requires_complete_exclusive_choices_and_rejects_controls():
    result = validate_title_declaration(
        complete_manifest(),
        dish_name="[side] Dish — extra",
        recognition="recognition",
        roles=["side", "side", "unknown"],
        no_role_tags=True,
        blockers=["side", "bad[marker]", "same", "same"],
        no_blockers=True,
    )

    rules = {error["rule"] for error in result.errors}
    assert {
        "title_control_character_forbidden",
        "title_boundary_ambiguous",
        "title_role_declaration_conflict",
        "unknown_title_role",
        "duplicate_title_role",
        "title_blocker_declaration_conflict",
        "reserved_title_blocker",
        "invalid_title_blocker",
        "duplicate_title_blocker",
    } <= rules


def test_title_parser_rejects_ambiguous_and_noncanonical_marker_order():
    manifest = complete_manifest()

    ambiguous = parse_canonical_title("Dish — one — two", manifest)
    wrong_order = parse_canonical_title(
        "[comparison] [side] Dish — recognition", manifest
    )
    role_after_blocker = parse_canonical_title(
        "[buy tofu] [side] Dish — recognition", manifest
    )

    assert "title_boundary_ambiguous" in {e["rule"] for e in ambiguous.errors}
    assert "title_role_order_noncanonical" in {
        e["rule"] for e in wrong_order.errors
    }
    assert "title_role_after_blocker" in {
        e["rule"] for e in role_after_blocker.errors
    }


def test_complete_manifest_requires_title_grammar_but_planning_forbids_it():
    complete = complete_manifest()
    validate_manifest_shape(
        complete,
        expected_kind="complete_task",
        filename="complete.json",
    )

    planning = dict(release_fixture().manifests["planning"])
    planning["title"] = complete["title"]
    try:
        validate_manifest_shape(
            planning,
            expected_kind="planning",
            filename="planning.json",
        )
    except Exception as exc:
        assert getattr(exc, "rule", None) == "manifest_malformed"
    else:
        raise AssertionError("planning manifest unexpectedly accepted title grammar")


def test_schema_upgrade_backfills_legacy_notes_write_marker(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(MIGRATIONS[1])
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
    )
    conn.execute(
        "PRAGMA user_version = 1"
    )
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, status, created_at, notes_written_at
        ) VALUES (
            'legacy', 'task', 'planning', 'old', 'commit', '{}', '{}',
            'claude', 'claude', 'written', 'created', 'legacy-written'
        )
        """
    )
    conn.commit()
    conn.close()

    upgraded = initialize_database(db_path)
    row = upgraded.execute(
        "SELECT task_content_written_at FROM submissions WHERE submission_id='legacy'"
    ).fetchone()

    assert row["task_content_written_at"] == "legacy-written"


def test_change_start_requires_canonical_live_title(tmp_path):
    app = make_app(tmp_path, Backend(title="Working title", notes=COMPLETE_NOTE))

    rejected = app.execute(
        "start",
        agent="claude",
        task_gid="task",
        kind="change",
        change_level="small",
        change_reason="adjust seasoning",
    )

    assert rejected["code"] == "VALIDATION_FAILED"
    assert "title_boundary_ambiguous" in {
        error["rule"] for error in rejected["errors"]
    }


def test_planning_preserves_working_title_and_rejects_title_declaration(tmp_path):
    backend = Backend(title="Free working title", notes="")
    app = make_app(tmp_path, backend)
    started = app.execute("start", agent="claude", task_gid="task", kind="planning")
    candidate = write_candidate(tmp_path, "planning.md", PLANNING_NOTE)

    rejected = app.execute(
        "prepare", model="gpt-5.6-sol",
        agent="claude",
        submission_id=started["submission_id"],
        file_path=candidate,
        **full_title_args(),
    )
    assert rejected["code"] == "VALIDATION_FAILED"
    assert "planning_title_declaration_forbidden" in {
        error["rule"] for error in rejected["errors"]
    }

    prepared = app.execute(
        "prepare", model="gpt-5.6-sol",
        agent="claude",
        submission_id=started["submission_id"],
        file_path=candidate,
    )
    assert prepared["data"]["prepared_title"] == {
        "raw": "Free working title",
        "fields": None,
    }


def test_verifier_can_replace_only_a_complete_structured_title(tmp_path):
    backend = Backend()
    app = make_app(tmp_path, backend)
    started = app.execute("start", agent="claude", task_gid="task", kind="initial")
    candidate = write_candidate(tmp_path, "complete.md", COMPLETE_NOTE)
    prepared = app.execute(
        "prepare", model="gpt-5.6-sol",
        agent="claude",
        submission_id=started["submission_id"],
        file_path=candidate,
        **full_title_args(),
    )
    assert prepared["state"] == "awaiting_verification"

    partial = app.execute(
        "approve", model="gpt-5.6-sol",
        agent="gpt",
        submission_id=started["submission_id"],
        file_path=candidate,
        correction="small",
        dish_name="Replacement",
    )
    assert partial["code"] == "VALIDATION_FAILED"
    assert {"title_recognition_required", "title_role_declaration_required"} <= {
        error["rule"] for error in partial["errors"]
    }

    replacement = full_title_args(
        dish_name="Tofu with chile bean",
        recognition="a comparison version with silken tofu",
        roles=["comparison"],
        blockers=[],
        no_blockers=True,
    )
    approved = app.execute(
        "approve", model="gpt-5.6-sol",
        agent="gpt",
        submission_id=started["submission_id"],
        file_path=candidate,
        correction="small",
        **replacement,
    )
    expected_title = (
        "[comparison] Tofu with chile bean — "
        "a comparison version with silken tofu"
    )
    assert approved["data"]["prepared_title"]["raw"] == expected_title

    submitted = app.execute(
        "submit",
        submission_id=started["submission_id"],
        file_path=candidate,
    )
    assert submitted["state"] == "consumed"
    assert backend.content_writes == [("task", expected_title, COMPLETE_NOTE)]


def test_backend_sends_title_and_notes_in_one_mutation(monkeypatch):
    calls = []

    class TasksApi:
        def __init__(self, client):
            pass

        def update_task(self, body, task_gid, opts, **kwargs):
            calls.append((body, task_gid, opts))
            return {"data": {"gid": task_gid}}

    monkeypatch.setattr(asana, "TasksApi", TasksApi)

    AsanaBackend(api_client=object()).update_task_content(
        task_gid="task", title="Dish — recognition", notes="complete notes"
    )

    assert calls == [
        (
            {"data": {"name": "Dish — recognition", "notes": "complete notes"}},
            "task",
            {"opt_fields": "gid"},
        )
    ]


def test_read_reports_noncanonical_title_without_blocking(tmp_path):
    app = make_app(tmp_path, Backend(title="Working title"))

    result = app.execute("read", agent="gpt", task_gid="task")

    structured = result["data"]["structured_title"]
    assert result["ok"] is True
    assert structured["raw"] == "Working title"
    assert structured["canonical"] is False
    assert structured["fields"] is None
    assert structured["errors"]
