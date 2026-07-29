import sys
from pathlib import Path

import pytest

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
    def __init__(self, title="Bare", notes="", section="rq", completed=False):
        self.title, self.notes, self.section, self.completed = title, notes, section, completed
        self.sections = [{"gid":"rq","name":"Research Queue"},{"gid":"vq","name":"Verification Queue"},{"gid":"12345","name":"Sichuan"},{"gid":"ref","name":"Reference"},{"gid":"src","name":"Sourcing"}]
        self.writes = 0; self.moves = 0
    def list_sections(self, project_gid): return self.sections
    def read_task(self, gid):
        return {"gid":gid,"name":self.title,"notes":self.notes,"completed":self.completed,"modified_at":"now","projects":[{"gid":COOKING_PROJECT_GID}],"memberships":[{"project":{"gid":COOKING_PROJECT_GID},"section":{"gid":self.section}}]}
    def update_task_content(self, *, task_gid, title, notes): self.writes += 1; self.title, self.notes = title, notes
    def update_task_completed(self, *, task_gid, completed): self.writes += 1; self.completed = completed
    def move_task_to_section(self, *, task_gid, section_gid): self.moves += 1; self.section=section_gid

def release(root, role=None):
    return ResolvedRelease(version="1.0.10", commit="", root=root, protocols={} if role is None else {role:f"{role} protocol"}, manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}", migration_metadata={}, requested_protocol_role=role)

def app(tmp_path, backend):
    honest = tmp_path / "honest"; honest.mkdir(exist_ok=True)
    (honest / "dish-verification-protocol.md").write_text("verification protocol")
    return DishApplication(initialize_database(tmp_path/"d.db"), backend, release_loader=lambda role=None: release(honest, role))

def write(tmp_path, name, text):
    p=tmp_path/name; p.write_text(text); return str(p)

def test_planning_prepare_writes_live_and_preserves_research_queue(tmp_path):
    b=Backend(); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="planning",change_level=None,change_reason=None)
    result=a.execute("prepare", model="gpt-5.6-sol",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"p.txt",PLANNING))
    assert result["ok"] and b.writes == 1 and b.section == "rq"
    assert "Locks: Keep crisp" in b.notes and "Exemptions: None" in b.notes
    assert result["allowed_actions"] == ["start"]
    assert result["data"]["required_start_kind"] == "initial"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
    ]

def test_research_prepare_writes_pending_then_moves_and_freezes_cycle(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    result=a.execute("prepare", agent="gpt",model="gpt-5.6-sol",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["ok"] and b.writes == 1 and b.moves == 1 and b.section == "vq"
    assert "Status: pending-verification" in b.notes
    assert "Verification protocol release: sha256:" in b.notes
    assert result["data"]["verification_cycle"]["protocol_release"].startswith("sha256:")
    assert result["allowed_actions"] == ["start"]
    assert result["data"]["required_start_kind"] == "verification"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
    ]
    assert "agent-semantic-review" not in result["data"]["validation_scope"]
    normalization = result["data"]["content_normalization"]
    assert normalization["applied"] is True
    assert {
        "Status", "Status detail", "Verification protocol release",
        "Researched by", "Self-verified",
    }.issubset(normalization["tool_owned_fields"])
    assert normalization["submitted_candidate_identity_is_authoritative"] is False
    assert "after these tool-owned process-field normalizations" in normalization["identity_scope"]
    verification = a.execute(
        "start", agent="codex", task_gid="t", kind="verification",
        run_id="fresh-verification-run",
        independence_attestation="independent",
    )
    assert verification["ok"]
    assert verification["allowed_actions"] == ["inspect"]

def test_planning_prepare_reports_every_missing_field_and_required_label(tmp_path):
    b = Backend("Planning task", "")
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    incomplete = PLANNING.replace("Research emphasis: Compare two hydration levels\n", "").replace(
        "Destination section: Sichuan — 12345\n", ""
    )
    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "missing-planning.txt", incomplete),
    )
    assert result["code"] == "VALIDATION_FAILED"
    missing = [
        item for item in result["errors"]
        if item.get("rule") == "planning_field_missing" and "field" in item
    ]
    assert missing == [
        {
            "rule": "planning_field_missing",
            "field": "Research emphasis",
            "required_label": "Research emphasis: <value>",
        },
        {
            "rule": "planning_field_missing",
            "field": "Destination section",
            "required_label": "Destination section: <value>",
        },
    ]


@pytest.mark.parametrize("field_name", ["Dish candidate", "Purpose", "Priors"])
def test_planning_prepare_rejects_empty_required_values_before_write(
    tmp_path, field_name
):
    b = Backend("Planning task", "")
    a = app(tmp_path, b)
    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning",
        change_level=None, change_reason=None,
    )
    candidate = "\n".join(
        f"{field_name}:" if line.startswith(f"{field_name}:") else line
        for line in PLANNING.splitlines()
    )

    result = a.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "empty-planning-field.txt", candidate),
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["errors"] == [
        {
            "rule": "planning.field-empty",
            "kind": "syntax",
            "message": f"{field_name} requires a non-empty value",
            "location": field_name,
        }
    ]
    assert b.writes == 0


def test_initial_start_rejects_empty_planning_purpose_before_operation(tmp_path):
    candidate = TASK.replace("Purpose: Compare texture", "Purpose:")
    lines = candidate.splitlines()
    b = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    a = app(tmp_path, b)
    result = a.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None,
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert any(
        error.get("rule") == "planning.field-empty"
        and error.get("location") == "Purpose"
        for error in result["errors"]
    )
    assert result["submission_id"] is None
    assert b.writes == 0
    assert b.moves == 0


def test_initial_prepare_requires_model(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    result=a.execute("prepare", agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_required"
    assert result["data"]["validation_scope"] == [
        "structural-only", "transition-state", "exact-content-identity",
    ]
    assert b.writes == 0

def test_initial_prepare_rejects_model_with_em_dash(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    result=a.execute("prepare", agent="gpt",model="gpt — 5.6",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_invalid_characters"
    assert b.writes == 0

def test_initial_prepare_rejects_model_with_comma(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    result=a.execute("prepare", agent="gpt",model="gpt-5.6, sol",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["code"] == "INVALID_ARGUMENT" and result["errors"][0]["rule"] == "model_invalid_characters"
    assert b.writes == 0

def test_stale_baseline_blocks_before_write(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    b.title = b.title + " changed"
    result=a.execute("prepare", model="gpt-5.6-sol",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["code"] == "CONFLICT" and b.writes == 0 and b.moves == 0


def test_prepare_rejects_placement_drift_for_all_operation_kinds(tmp_path):
    import pytest
    from dish_tool.database import confirm_task_content, create_operation
    from dish_tool.errors import DishRuleError
    from dish_tool.models import OperationActors
    from dish_tool.step6 import prepare_live

    for kind in ("planning", "initial", "change"):
        case = tmp_path / kind
        case.mkdir()
        if kind == "planning":
            b = Backend()
            candidate_text = PLANNING
        else:
            lines = TASK.splitlines()
            b = Backend(lines[0], "\n".join(lines[1:]) + "\n")
            candidate_text = TASK
        a = app(case, b)
        confirm_task_content(
            a.conn,
            task_gid="t",
            title=b.title,
            notes=b.notes,
            schema_version="2",
            boundary="placement-drift-test",
        )
        actors = OperationActors(
            editor_agent="gpt" if kind in {"planning", "change"} else None,
            researcher_agent="gpt" if kind == "initial" else None,
            run_id=f"{kind}-run",
        )
        op = create_operation(
            a.conn,
            task_gid="t",
            operation_kind=kind,
            expected_identity=a.conn.execute(
                "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid='t'"
            ).fetchone()[0],
            expected_section_gid="rq",
            schema_version="2",
            actors=actors,
        )
        b.section = "12345"
        candidate = write(case, "candidate.txt", candidate_text)
        with pytest.raises(DishRuleError) as exc:
            prepare_live(
                a.conn,
                b,
                operation_id=op["operation_id"],
                agent="gpt",
                model="gpt-5.6-sol",
                file_path=candidate,
                release=release(case / "honest"),
                material_classification="non-material" if kind == "change" else None,
            )
        assert exc.value.rule == "live_task_placement_drift"
        assert b.writes == 0
        assert b.moves == 0


def test_completed_task_requires_audited_marco_reopen_before_planning(tmp_path):
    from dish_tool.admin import DishAdminApplication

    b = Backend(completed=True)
    a = app(tmp_path, b)
    blocked = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning", run_id="plan-run"
    )
    assert blocked["code"] == "WRONG_STATE"
    assert blocked["errors"][0]["rule"] == "planning_completed_task_reopen_required"
    assert blocked["data"]["required_admin_action"] == "reopen-planning"
    assert blocked["data"]["resolver"] == "Marco/admin reopen-planning"
    assert blocked["data"]["legal_next_step"] == (
        "Marco/admin runs reopen-planning with a reason; after it succeeds, "
        "retry start with kind=planning using a fresh client.request_id"
    )
    assert a.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0

    admin = DishAdminApplication(
        a.conn,
        backend=b,
        release_loader=lambda: a._load_release(None),
        invocation_request_id="reopen-request",
        invocation_run_id="marco-run",
    )
    reopened = admin.execute(
        "reopen-planning", task_gid="t", reason="cook this dish again"
    )
    assert reopened["ok"]
    assert reopened["allowed_actions"] == ["start"]
    assert reopened["data"]["required_start_kind"] == "planning"
    assert b.completed is False
    attempt = a.conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE task_gid='t'"
    ).fetchone()
    assert attempt["outcome"] == "confirmed"
    assert attempt["reason"] == "cook this dish again"
    assert attempt["actor_run_id"] == "marco-run"
    assert attempt["request_id"] == "reopen-request"
    audit = a.conn.execute(
        "SELECT event_type,actor_provenance FROM audit_events WHERE task_gid='t' AND event_type='planning.task_reopened'"
    ).fetchone()
    assert audit is not None
    assert "marco-run" in audit["actor_provenance"]

    started = a.execute(
        "start", agent="gpt", task_gid="t", kind="planning", run_id="plan-run"
    )
    assert started["ok"]


def test_planning_reopen_rejects_non_bare_completed_task(tmp_path):
    from dish_tool.admin import DishAdminApplication

    b = Backend(notes="not bare", completed=True)
    a = app(tmp_path, b)
    admin = DishAdminApplication(
        a.conn, backend=b, release_loader=lambda: a._load_release(None),
        invocation_run_id="marco-run",
    )
    result = admin.execute("reopen-planning", task_gid="t", reason="retry")
    assert result["code"] == "VALIDATION_FAILED"
    assert any(
        error["rule"] == "planning_reopen_notes_not_empty" for error in result["errors"]
    )
    assert b.completed is True
    assert a.conn.execute("SELECT COUNT(*) FROM planning_reopen_attempts").fetchone()[0] == 0
