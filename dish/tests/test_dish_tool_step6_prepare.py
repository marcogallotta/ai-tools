import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

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

PLANNING = """### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
"""

class Backend:
    def __init__(self, title="Bare", notes="", section="rq"):
        self.title, self.notes, self.section = title, notes, section
        self.sections = [{"gid":"rq","name":"Research Queue"},{"gid":"vq","name":"Verification Queue"},{"gid":"12345","name":"Sichuan"},{"gid":"ref","name":"Reference"},{"gid":"src","name":"Sourcing"}]
        self.writes = 0; self.moves = 0
    def list_sections(self, project_gid): return self.sections
    def read_task(self, gid):
        return {"gid":gid,"name":self.title,"notes":self.notes,"completed":False,"modified_at":"now","projects":[{"gid":COOKING_PROJECT_GID}],"memberships":[{"project":{"gid":COOKING_PROJECT_GID},"section":{"gid":self.section}}]}
    def update_task_content(self, *, task_gid, title, notes): self.writes += 1; self.title, self.notes = title, notes
    def move_task_to_section(self, *, task_gid, section_gid): self.moves += 1; self.section=section_gid

def release(root, role=None):
    return ResolvedRelease(version="1.0.4", commit="", root=root, protocols={} if role is None else {role:f"{role} protocol"}, manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}", migration_metadata={}, requested_protocol_role=role)

def app(tmp_path, backend):
    honest = tmp_path / "honest"; honest.mkdir(exist_ok=True)
    (honest / "dish-verification-protocol.md").write_text("verification protocol")
    return DishApplication(initialize_database(tmp_path/"d.db"), backend, release_loader=lambda role=None: release(honest, role))

def write(tmp_path, name, text):
    p=tmp_path/name; p.write_text(text); return str(p)

def test_planning_prepare_writes_live_and_preserves_research_queue(tmp_path):
    b=Backend(); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="planning",change_level=None,change_reason=None)
    result=a.execute("prepare",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"p.txt",PLANNING))
    assert result["ok"] and b.writes == 1 and b.section == "rq"
    assert "Locks: Keep crisp" in b.notes and "Exemptions: None" in b.notes

def test_research_prepare_writes_pending_then_moves_and_freezes_cycle(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    result=a.execute("prepare",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["ok"] and b.writes == 1 and b.moves == 1 and b.section == "vq"
    assert "Status: pending-verification" in b.notes
    assert "Verification protocol release: sha256:" in b.notes
    assert result["data"]["verification_cycle"]["protocol_release"].startswith("sha256:")

def test_stale_baseline_blocks_before_write(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    b.title = b.title + " changed"
    result=a.execute("prepare",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["code"] == "CONFLICT" and b.writes == 0 and b.moves == 0
