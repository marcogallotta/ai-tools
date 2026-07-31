
"""Shared helpers extracted from test_dish_tool_step7_verification.py."""


import pytest


from pathlib import Path

from dish_tool.commands import DishApplication

from dish_tool.constants import COOKING_PROJECT_GID

from dish_tool.database import initialize_database

from dish_tool.models import ResolvedRelease

TASK = """[non-main] Test dish — crisp comparison side
A compact side dish for testing texture.
WHY COOK IT
Compare hydration routes.
## WHAT TO BUY
None - pantry snapshot lists required items in stock
## QUANTITIES
Portions: one sitting
100 g test ingredient
## HOW TO COOK IT
1. Cook it.
## WHAT SUCCESS LOOKS LIKE
Crisp and aromatic.
---
## PROCESS RECORD
Status: pending-research
Status detail: Continue research
Resume status: None
Verification protocol release: None
Researched by: ChatGPT — GPT-5, 2026-07-25
Verified by: None
Self-verified: ChatGPT — GPT-5, 2026-07-25
### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
### Research basis
Classification: Source-backed dish
source.example/test — Construction — hydration ratio — selected route is drier
Schema version: 2
"""

class Backend:
    def __init__(self):
        lines = TASK.splitlines()
        self.title = lines[0]
        self.notes = "\n".join(lines[1:]) + "\n"
        self.section = "rq"
        self.writes = 0
        self.moves = 0
        self.sections = [
            {"gid": "rq", "name": "Research Queue"},
            {"gid": "vq", "name": "Verification Queue"},
            {"gid": "12345", "name": "Sichuan"},
            {"gid": "ref", "name": "Reference"},
            {"gid": "src", "name": "Sourcing"},
        ]

    def list_sections(self, project_gid): return self.sections
    def read_task(self, gid):
        return {"gid": gid, "name": self.title, "notes": self.notes, "completed": False,
                "modified_at": "now", "projects": [{"gid": COOKING_PROJECT_GID}],
                "memberships": [{"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": self.section}}]}
    def update_task_content(self, *, task_gid, title, notes):
        self.writes += 1; self.title, self.notes = title, notes
    def move_task_to_section(self, *, task_gid, section_gid):
        self.moves += 1; self.section = section_gid

def make_app(tmp_path):
    backend = Backend()
    honest = tmp_path / "honest"; honest.mkdir()
    verification_text = "# Exact frozen Verification protocol\n"
    (honest / "dish-verification-protocol.md").write_text(verification_text)
    def release(role=None):
        return ResolvedRelease(version="1.0.10", commit="", root=honest,
            protocols={} if role is None else {role: verification_text if role == "verification" else f"{role} protocol"},
            manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}",
            migration_metadata={}, requested_protocol_role=role)
    app = DishApplication(initialize_database(tmp_path / "dish.db"), backend, release_loader=release)
    candidate = tmp_path / "candidate.txt"; candidate.write_text(TASK)
    start = app.execute("start", agent="gpt", task_gid="t", kind="initial", change_level=None, change_reason=None, run_id="constructor-run")
    prepared = app.execute("prepare", agent="gpt", model="gpt-5.6-sol", submission_id=start["submission_id"], file_path=str(candidate))
    assert prepared["ok"]
    return app, backend, start["submission_id"], verification_text
