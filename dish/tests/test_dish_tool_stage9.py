"""Stage 9: tool-aware release documentation and exact-bundle integration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN_DIR))

from dish_tool.commands import DishApplication  # noqa: E402
from dish_tool.database import initialize_database  # noqa: E402
from dish_tool.models import ResolvedRelease  # noqa: E402
from dish_tool.validation import validate_manifest_shape  # noqa: E402


BUNDLE_DIR = Path(__file__).parent / "fixtures" / "protocol-release-tool-aware"
DOCS_DIR = BIN_DIR / "docs"
ACTIVATION_PATH = DOCS_DIR / "runtime-contract.md"
IMPLEMENTATION_PLAN_PATH = DOCS_DIR / "dish-tool-imp.md"
GLOBAL_GUIDE_PATH = Path(__file__).parents[2] / "CLAUDE-global.md"

SECTIONS = [
    {"gid": "research", "name": "Research Queue"},
    {"gid": "verification", "name": "Verification Queue"},
    {"gid": "123456", "name": "Weeknight"},
    {"gid": "sourcing", "name": "Sourcing"},
    {"gid": "reference", "name": "Reference"},
]

PLANNING_NOTE = """# PLANNING BRIEF
Destination section: Weeknight (123456)
Exemptions: None
Research notes: Build a complete weeknight dish task.
"""

PREPARED_COMPLETE_NOTE = """# DISH
Exemptions: None
Destination section: Weeknight (123456)
Self-verified: claude, 2026-07-21
Verification: Pending opposite-family verification
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

APPROVED_COMPLETE_NOTE = PREPARED_COMPLETE_NOTE.replace(
    "Verification: Pending opposite-family verification",
    "Verification: gpt, 2026-07-21",
)


def load_tool_aware_release() -> ResolvedRelease:
    version = (BUNDLE_DIR / "protocol_release").read_text(encoding="utf-8").strip()
    planning_text = (BUNDLE_DIR / "dish-planning-manifest.json").read_text(
        encoding="utf-8"
    )
    complete_text = (BUNDLE_DIR / "dish-complete-task-manifest.json").read_text(
        encoding="utf-8"
    )
    return ResolvedRelease(
        version=version,
        commit="tool-aware-fixture-commit",
        root=BUNDLE_DIR,
        protocols={
            "planning": (BUNDLE_DIR / "dish-planning-protocol.md").read_text(
                encoding="utf-8"
            ),
            "research": (BUNDLE_DIR / "dish-research-protocol.md").read_text(
                encoding="utf-8"
            ),
            "verification": (
                BUNDLE_DIR / "dish-verification-protocol.md"
            ).read_text(encoding="utf-8"),
        },
        manifests={
            "planning": json.loads(planning_text),
            "complete_task": json.loads(complete_text),
        },
        schema_version="1",
        schema={"schema_kind": "dish-task"},
        manifest_texts={
            "planning": planning_text,
            "complete_task": complete_text,
        },
    )


def cooking_task(task_gid: str, notes: str, section_gid: str = "research") -> dict:
    section_name = next(
        section["name"] for section in SECTIONS if section["gid"] == section_gid
    )
    return {
        "gid": task_gid,
        "name": CANONICAL_TITLE,
        "notes": notes,
        "completed": False,
        "projects": [{"gid": "1215089183018968", "name": "Cooking"}],
        "memberships": [
            {
                "project": {"gid": "1215089183018968", "name": "Cooking"},
                "section": {"gid": section_gid, "name": section_name},
            }
        ],
    }


class FakeBackend:
    def __init__(self, task: dict):
        self.task = dict(task)
        self.content_writes: list[tuple[str, str]] = []
        self.moves: list[str] = []

    def list_sections(self, project_gid: str) -> list[dict]:
        assert project_gid == "1215089183018968"
        return list(SECTIONS)

    def read_task(self, task_gid: str) -> dict:
        assert task_gid == self.task["gid"]
        return dict(self.task)

    def create_bare_task(self, **kwargs):
        raise AssertionError("creation is outside this integration fixture")

    def update_task_content(
        self, *, task_gid: str, title: str, notes: str
    ) -> None:
        assert task_gid == self.task["gid"]
        self.content_writes.append((title, notes))
        self.task["name"] = title
        self.task["notes"] = notes

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None:
        assert task_gid == self.task["gid"]
        self.moves.append(section_gid)
        section_name = next(
            section["name"] for section in SECTIONS if section["gid"] == section_gid
        )
        self.task["memberships"][0]["section"] = {
            "gid": section_gid,
            "name": section_name,
        }


def make_app(tmp_path: Path, task: dict) -> tuple[DishApplication, FakeBackend]:
    backend = FakeBackend(task)
    application = DishApplication(
        initialize_database(tmp_path / "dish.db"),
        backend,
        release_loader=load_tool_aware_release,
    )
    return application, backend


def write_candidate(tmp_path: Path, name: str, text: str) -> str:
    candidate = tmp_path / name
    candidate.write_text(text, encoding="utf-8")
    return str(candidate)


def test_tool_aware_beta_bundle_is_complete_consistent_and_agent_only():
    expected = {
        "dish-planning-protocol.md",
        "dish-research-protocol.md",
        "dish-verification-protocol.md",
        "dish-planning-manifest.json",
        "dish-complete-task-manifest.json",
        "protocol_release",
    }
    assert {path.name for path in BUNDLE_DIR.iterdir()} == expected

    release = load_tool_aware_release()
    assert release.version == "fixture-v1b-structured-title"
    assert release.manifests["planning"]["protocol_release"] == release.version
    assert release.manifests["complete_task"]["protocol_release"] == release.version
    validate_manifest_shape(
        release.manifests["planning"],
        expected_kind="planning",
        filename="dish-planning-manifest.json",
    )
    validate_manifest_shape(
        release.manifests["complete_task"],
        expected_kind="complete_task",
        filename="dish-complete-task-manifest.json",
    )

    combined = "\n".join(release.protocols.values()).lower()
    assert "dish start" in combined
    assert "dish prepare" in combined
    assert "dish submit" in combined
    assert "--dish-name" in combined
    assert "--recognition" in combined
    assert "--no-role-tags" in combined
    assert "--no-blockers" in combined
    assert "dish-admin" not in combined
    assert "bin/asana" not in combined
    assert "asana set-notes" not in combined
    assert "batch-apply" not in combined




def test_activation_document_is_the_complete_local_operating_contract():
    runbook = ACTIVATION_PATH.read_text(encoding="utf-8").lower()
    assert "authority and scope" in runbook
    assert "json response contract" in runbook
    assert "result codes and exit statuses" in runbook
    assert "rerun rules" in runbook
    assert "troubleshooting checklist" in runbook
    assert "tool/protocol disagreement" in runbook
    assert "single-agent local test path" in runbook
    assert "step 11" in runbook

def test_global_guide_does_not_expose_the_development_tool():
    guide = GLOBAL_GUIDE_PATH.read_text(encoding="utf-8").lower()
    assert "dish" not in guide


def test_implementation_plan_defers_production_rollout():
    plan = IMPLEMENTATION_PLAN_PATH.read_text(encoding="utf-8").lower()
    rollout = plan.split("## rollout to production", maxsplit=1)[1]
    assert "separate future authorization required" in plan
    assert "no live cooking-task write" in plan
    assert "`claude-global.md`" in rollout
    assert "snapshot-safe migration" in rollout
    assert "mixed production authority" in rollout


def test_planning_workflow_uses_exact_tool_aware_bundle_end_to_end(tmp_path):
    application, backend = make_app(tmp_path, cooking_task("planning-task", ""))

    started = application.execute(
        "start",
        agent="claude",
        task_gid="planning-task",
        kind="planning",
    )
    assert started["ok"] is True
    compatibility = started["data"]["current_compatibility"]
    assert compatibility["protocol_version"] == "fixture-v1b-structured-title"
    assert compatibility["schema_version"] == "1"
    assert compatibility["stage_protocol"]["planning"] == (
        BUNDLE_DIR / "dish-planning-protocol.md"
    ).read_text(encoding="utf-8")
    assert compatibility["legacy_validation_adapter_text"] == (
        BUNDLE_DIR / "dish-planning-manifest.json"
    ).read_text(encoding="utf-8")

    candidate = write_candidate(tmp_path, "planning.md", PLANNING_NOTE)
    prepared = application.execute(
        "prepare", model="gpt-5.6-sol",
        agent="claude",
        submission_id=started["submission_id"],
        file_path=candidate,
    )
    assert prepared["state"] == "ready"

    submitted = application.execute(
        "submit",
        submission_id=started["submission_id"],
        file_path=candidate,
    )
    assert submitted["state"] == "consumed"
    assert submitted["data"]["handoff"] == "planning_research_queue"
    assert backend.content_writes == [(CANONICAL_TITLE, PLANNING_NOTE)]
    assert backend.moves == []


def test_initial_workflow_uses_exact_tool_aware_bundle_end_to_end(tmp_path):
    application, backend = make_app(
        tmp_path, cooking_task("initial-task", PLANNING_NOTE)
    )

    started = application.execute(
        "start",
        agent="claude",
        task_gid="initial-task",
        kind="initial",
    )
    assert started["state"] == "drafting"
    compatibility = started["data"]["current_compatibility"]
    assert compatibility["protocol_version"] == "fixture-v1b-structured-title"
    assert compatibility["schema_version"] == "1"
    assert compatibility["stage_protocol"] == {
        "research": (BUNDLE_DIR / "dish-research-protocol.md").read_text(
            encoding="utf-8"
        )
    }
    assert compatibility["legacy_validation_adapter_text"] == (
        BUNDLE_DIR / "dish-complete-task-manifest.json"
    ).read_text(encoding="utf-8")

    prepared_candidate = write_candidate(
        tmp_path, "complete-prepared.md", PREPARED_COMPLETE_NOTE
    )
    prepared = application.execute(
        "prepare", model="gpt-5.6-sol",
        agent="claude",
        submission_id=started["submission_id"],
        file_path=prepared_candidate,
        **TITLE_ARGS,
    )
    assert prepared["state"] == "awaiting_verification"
    assert backend.moves == ["verification"]

    approved_candidate = write_candidate(
        tmp_path, "complete-approved.md", APPROVED_COMPLETE_NOTE
    )
    approved = application.execute(
        "approve", model="gpt-5.6-sol",
        agent="gpt",
        submission_id=started["submission_id"],
        file_path=approved_candidate,
        correction="small",
    )
    assert approved["state"] == "ready"

    submitted = application.execute(
        "submit",
        submission_id=started["submission_id"],
        file_path=approved_candidate,
    )
    assert submitted["state"] == "consumed"
    assert submitted["data"]["handoff"] == "moved_to_destination"
    assert backend.content_writes == [(CANONICAL_TITLE, APPROVED_COMPLETE_NOTE)]
    assert backend.moves == ["verification", "123456"]
