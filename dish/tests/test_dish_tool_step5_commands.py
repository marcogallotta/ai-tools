import json
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_tool.admin import DishAdminApplication
from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import confirm_task_content, initialize_database
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
Status: pending-verification
Status detail: None
Resume status: None
Verification protocol release: abc123
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
    def __init__(self, title="Bare", notes="", section="rq"):
        self.title, self.notes, self.section = title, notes, section
        self.sections = [{"gid":"rq","name":"Research Queue"},{"gid":"vq","name":"Verification Queue"},{"gid":"12345","name":"Sichuan"},{"gid":"ref","name":"Reference"},{"gid":"src","name":"Sourcing"}]
    def list_sections(self, project_gid): return self.sections
    def read_task(self, gid):
        return {"gid":gid,"name":self.title,"notes":self.notes,"completed":False,"modified_at":"now","projects":[{"gid":COOKING_PROJECT_GID}],"memberships":[{"project":{"gid":COOKING_PROJECT_GID},"section":{"gid":self.section}}]}
    def create_bare_task(self, *, title, project_gid, section_gid): self.title=title; self.notes=""; self.section=section_gid; return {"gid":"new","name":title,"notes":""}
    def update_task_content(self, *, task_gid, title, notes): self.title, self.notes = title, notes
    def move_task_to_section(self, *, task_gid, section_gid): self.section=section_gid

def release(role=None, migrations=False):
    return ResolvedRelease(version="1.0.10", commit="", root=Path("."), protocols={} if role is None else {role:f"{role} protocol"}, manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}", migration_metadata={"m.json":{"migration_id":"m","from_schema_version":"1","to_schema_version":"2","protocol_version":"1.0.10","automatic":False,"description":"x","source_ids":["x"],"operations":[{"type":"canonical-parse-render","description":"test"}]}} if migrations else {}, requested_protocol_role=role)

def app(tmp_path, backend): return DishApplication(initialize_database(tmp_path/"d.db"), backend, release_loader=lambda role=None: release(role))

def test_sections_and_create_are_scoped_and_bare(tmp_path):
    b=Backend(); a=app(tmp_path,b)
    assert a.execute("sections",agent="claude")["data"]["project_gid"] == COOKING_PROJECT_GID
    made=a.execute("create",agent="claude",title="New dish")
    assert made["data"] == {"task_gid":"new","schema_version":"2","bare_task":True,"required_start_kind":"planning"}
    assert b.notes == "" and b.section == "rq"

def test_read_reports_exact_state_and_migration_required(tmp_path):
    lines=TASK.replace("Schema version: 2","Schema version: 1").splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["task"]["title"] == lines[0]
    assert result["data"]["migration_required"] is True
    assert result["data"]["content_identity"]

def test_read_bare_task_is_not_migration_required(tmp_path):
    b=Backend(); result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["parsed"] is None
    assert result["data"]["task_schema_version"] is None
    assert result["data"]["migration_required"] is False
    assert result["data"]["validation"] == []

def test_read_planning_stage_brief_is_not_migration_required(tmp_path):
    notes = (
        "### Planning brief\n"
        "Dish candidate: Test dish\n"
        "Purpose: Compare texture\n"
        "Role: non-main — small side for comparison\n"
        "Priors: None\n"
        "Locks: Keep crisp\n"
        "Exemptions: None\n"
        "Research emphasis: Compare two hydration levels\n"
        "Destination section: Sichuan — 12345\n"
    )
    b=Backend("Bare", notes)
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["parsed"] is None
    assert result["data"]["validation"] == []
    assert result["data"]["migration_required"] is False

def test_read_current_schema_canonical_task_is_not_migration_required(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["parsed"] is not None
    assert result["data"]["migration_required"] is False

def test_read_unparseable_task_with_no_schema_line_is_migration_required(tmp_path):
    lines=TASK.replace("Schema version: 2\n","").splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["parsed"] is None
    assert result["data"]["migration_required"] is True

def test_read_malformed_but_current_schema_task_is_not_migration_required(tmp_path):
    duplicated = TASK.replace(
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n",
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n"
        "## WHAT TO BUY\nNone - pantry snapshot lists required items in stock\n",
    )
    lines=duplicated.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["parsed"] is None
    assert any(v["rule"] == "section_duplicate" for v in result["data"]["validation"])
    assert result["data"]["migration_required"] is False

def test_read_canonical_document_with_corrupted_state_block_is_not_masked_as_planning_stage(tmp_path):
    # The Planning brief block is intact and would parse cleanly on its own, but
    # the document carries process-record markers ("---" / "## PROCESS RECORD"),
    # so it is asserting canonical shape. A broken state block here must still
    # be reported as a real finding, not silently reinterpreted as an
    # ordinary Planning-stage brief just because the brief substring is valid.
    corrupted = TASK.replace(
        "Status: pending-verification\n",
        "Status: pending-verification\nStatus: pending-verification\n",
    )
    lines=corrupted.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["parsed"] is None
    assert any(v["rule"] == "state_field_duplicate" for v in result["data"]["validation"])
    assert result["data"]["migration_required"] is False

def test_start_planning_on_bare_task_reports_no_diagnostics(tmp_path):
    b=Backend(); a=app(tmp_path,b)
    result=a.execute("start",agent="gpt",task_gid="t",kind="planning",change_level=None,change_reason=None)
    assert result["ok"]
    assert result["data"]["schema"]["diagnostics"] == []

def test_start_research_on_planning_stage_brief_reports_no_diagnostics(tmp_path):
    notes = (
        "### Planning brief\n"
        "Dish candidate: Test dish\n"
        "Purpose: Compare texture\n"
        "Role: non-main — small side for comparison\n"
        "Priors: None\n"
        "Locks: Keep crisp\n"
        "Exemptions: None\n"
        "Research emphasis: Compare two hydration levels\n"
        "Destination section: Sichuan — 12345\n"
    )
    b=Backend("Bare", notes); a=app(tmp_path,b)
    result=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    assert result["ok"]
    assert result["data"]["schema"]["diagnostics"] == []

def test_start_claims_once_and_returns_only_stage_protocol(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n") ; a=app(tmp_path,b)
    first=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    assert first["ok"] and first["data"]["protocol"] == {"role":"research","version":"1.0.10","text":"research protocol"}
    second=a.execute("start",agent="claude",task_gid="t",kind="initial",change_level=None,change_reason=None)
    assert second["code"] == "CONFLICT"
    inspected=a.execute("inspect",agent="claude",submission_id=first["submission_id"])
    assert inspected["data"]["operation"]["operation_kind"] == "initial"
    assert "protocol_bundle" not in inspected["data"]

def test_start_fails_closed_on_drift(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    confirm_task_content(a.conn,task_gid="t",title="different",notes=b.notes,schema_version="2")
    result=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    assert result["code"] == "CONFLICT"

def test_admin_migration_sets_version_only_on_validated_exact_write(tmp_path):
    lines=TASK.replace("Schema version: 2","Schema version: 1").splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    admin=DishAdminApplication(initialize_database(tmp_path/"d.db"), backend=b, release_loader=lambda: release(None,True))
    result=admin.execute("migrate",task_gid="t")
    assert result["ok"] and result["data"]["schema_version"] == "2"
    assert "Schema version: 2" in b.notes

def test_wrong_state_response_exposes_current_legal_action(tmp_path):
    b = Backend()
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    result = a.execute("submit", submission_id=started["submission_id"])
    assert result["code"] == "WRONG_STATE"
    assert result["allowed_actions"] == ["prepare"]
    assert result["retryable"] is False


def test_retryable_prepare_validation_exposes_prepare_action(tmp_path):
    b = Backend()
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    candidate = tmp_path / "invalid.txt"
    candidate.write_text("not a planning brief")
    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=str(candidate),
        no_role_tags=True, no_blockers=True,
    )
    assert not result["ok"]
    assert result["retryable"] is True
    assert result["allowed_actions"] == ["prepare"]

def test_read_exposes_active_operation_and_next_action(tmp_path):
    b = Backend()
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    result = a.execute("read", agent="gpt", task_gid="t")
    assert result["submission_id"] == started["submission_id"]
    assert result["state"] == "open"
    assert result["allowed_actions"] == ["prepare"]
    assert result["data"]["active_operation"]["submission_id"] == started["submission_id"]

