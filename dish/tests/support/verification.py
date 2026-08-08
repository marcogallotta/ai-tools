
"""Shared helpers extracted from test_dish_tool_step7_verification.py."""


import pytest


from pathlib import Path

from dish_tool.commands import DishApplication

from dish_tool.constants import COOKING_PROJECT_GID

from dish_tool.database_initialization import initialize_database

from dish_tool.models import ResolvedRelease

from tests.support.asana_backend import StatefulAsanaBackend

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

class Backend(StatefulAsanaBackend):
    def __init__(
        self,
        *,
        task_gid: str = "t",
        created_task_gid: str = "1000000000000001",
    ):
        lines = TASK.splitlines()
        super().__init__(
            title=lines[0],
            notes="\n".join(lines[1:]) + "\n",
            section="rq",
            task_gid=task_gid,
            created_task_gid=created_task_gid,
        )


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


def review_and_inspect(
    app,
    *,
    agent="codex",
    task_gid="t",
    run_id="review",
    operation_id=None,
):
    """Start a verification review and assert its public review surface."""
    result = app.execute(
        "start",
        agent=agent,
        task_gid=task_gid,
        kind="verification",
        run_id=run_id,
        independence_attestation="independent",
    )
    assert result["ok"]
    inspected = app.execute(
        "inspect",
        agent=agent,
        submission_id=operation_id or result["submission_id"],
    )
    assert inspected["ok"]
    assert inspected["allowed_actions"] == ["approve", "reject"]
    return result
