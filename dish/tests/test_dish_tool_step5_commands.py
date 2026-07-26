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
    return ResolvedRelease(version="1.0.9", commit="", root=Path("."), protocols={} if role is None else {role:f"{role} protocol"}, manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}", migration_metadata={"m.json":{"migration_id":"m","from_schema_version":"1","to_schema_version":"2","protocol_version":"1.0.9","automatic":False,"description":"x","source_ids":["x"],"operations":[{"type":"canonical-parse-render","description":"test"}]}} if migrations else {}, requested_protocol_role=role)

def app(tmp_path, backend): return DishApplication(initialize_database(tmp_path/"d.db"), backend, release_loader=lambda role=None: release(role))

def test_sections_and_create_are_scoped_and_bare(tmp_path):
    b=Backend(); a=app(tmp_path,b)
    assert a.execute("sections",agent="claude")["data"]["project_gid"] == COOKING_PROJECT_GID
    made=a.execute("create",agent="claude",title="New dish")
    assert made["data"] == {"task_gid":"new","schema_version":"2","bare_task":True}
    assert b.notes == "" and b.section == "rq"

def test_read_reports_exact_state_and_migration_required(tmp_path):
    lines=TASK.replace("Schema version: 2","Schema version: 1").splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n")
    result=app(tmp_path,b).execute("read",agent="gpt",task_gid="t")
    assert result["data"]["task"]["title"] == lines[0]
    assert result["data"]["migration_required"] is True
    assert result["data"]["content_identity"]

def test_start_claims_once_and_returns_only_stage_protocol(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n") ; a=app(tmp_path,b)
    first=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    assert first["ok"] and first["data"]["protocol"] == {"role":"research","version":"1.0.9","text":"research protocol"}
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
