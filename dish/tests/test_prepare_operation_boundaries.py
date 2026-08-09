from pathlib import Path

import pytest


from dish_tool.commands import DishApplication
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database_initialization import initialize_database
from dish_tool.models import ResolvedRelease
from tests.support.planning import (
    Backend,
    PLANNING,
    TASK,
    app,
    release,
    write,

)

_DUPLICATE_SCHEMA_CANDIDATE = TASK.replace(
    "Schema version: 2\n",
    "Schema version: 2\nSchema version: 2\n",
    1,
)

@pytest.mark.smoke
@pytest.mark.parametrize(
    ("candidate", "expected_rule", "expected_location", "expected_field"),
    [
        (
            TASK.replace("Purpose: Compare texture", "Purpose:"),
            "planning.field-empty",
            "Purpose",
            None,
        ),
        (
            TASK.replace(
                "Exemptions: None",
                "Exemptions: [nutrition-sodium] — Marco approved for this dish",
            ),
            "planning.exemption-tag-unsupported",
            "Exemptions",
            None,
        ),
        (
            TASK.replace(
                "Destination section: Sichuan — 12345",
                "Destination section: Sichuan — 12345\n"
                "Serving note: hidden unsupported field",
            ),
            "planning_field_unknown",
            None,
            "Serving note",
        ),
    ],
)
def test_initial_start_rejects_invalid_planning_brief_before_operation(
    tmp_path, candidate, expected_rule, expected_location, expected_field
):
    lines = candidate.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)

    result = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None,
    )

    assert result["code"] == "VALIDATION_FAILED"
    matching = [error for error in result["errors"] if error.get("rule") == expected_rule]
    assert matching
    if expected_location is not None:
        assert matching[0].get("location") == expected_location
    if expected_field is not None:
        assert matching[0].get("field") == expected_field
    assert result["submission_id"] is None
    assert backend.writes == 0
    assert backend.moves == 0
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("model", "expected_rule", "expects_validation_scope"),
    [
        (None, "model_required", True),
        ("gpt — 5.6", "model_invalid_characters", False),
        ("gpt-5.6, sol", "model_invalid_characters", False),
    ],
)
def test_initial_prepare_rejects_missing_or_invalid_model(
    tmp_path, model, expected_rule, expects_validation_scope
):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None,
    )
    arguments = {
        "agent": "gpt",
        "submission_id": started["submission_id"],
        "file_path": write(tmp_path, "c.txt", TASK),
    }
    if model is not None:
        arguments["model"] = model

    result = application.execute("prepare", **arguments)

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == expected_rule
    if expects_validation_scope:
        assert result["data"]["validation_scope"] == [
            "structural-only", "transition-state", "exact-content-identity",
        ]
    assert backend.writes == 0


@pytest.mark.smoke
def test_initial_prepare_agent_attested_marco_decision_requires_explicit_attestation(tmp_path):
    import json

    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None, run_id="research-run",
    )
    candidate_text = TASK.replace(
        "### Research basis",
        "### Decisions\nHuman — Marco: Use chicken.\n### Research basis",
    )
    candidate_path = write(tmp_path, "decision-candidate.txt", candidate_text)

    preflight = application.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=candidate_path,
    )
    assert preflight["code"] == "CONFIRMATION_REQUIRED"
    assert preflight["errors"][0]["rule"] == "decision_attestation_required"
    assert backend.writes == 0

    result = application.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=candidate_path,
        governed_change_fields=["Decisions"],
    )
    assert result["ok"]
    assert "Human — Marco: Use chicken." in backend.notes
    assert application.conn.execute(
        "SELECT COUNT(*) FROM marco_authorizations WHERE operation_id=?",
        (started["submission_id"],),
    ).fetchone()[0] == 0
    attestation = application.conn.execute(
        """SELECT actor_agent,actor_provenance,details
             FROM audit_events
            WHERE operation_id=? AND event_type='decision.agent_attested'
            ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (started["submission_id"],),
    ).fetchone()
    provenance = json.loads(attestation["actor_provenance"])
    details = json.loads(attestation["details"])
    assert attestation["actor_agent"] == "gpt"
    assert provenance["run_id"] == "research-run"
    assert provenance["source"] == "agent-attested-conversation"
    assert details["appended_decisions"] == ["Human — Marco: Use chicken."]
    assert details["formal_marco_authorization"] is False


@pytest.mark.smoke
def test_first_canonical_research_candidate_can_attest_marco_decision_from_planning_brief(tmp_path):
    import json

    backend = Backend(TASK.splitlines()[0], PLANNING)
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None, run_id="research-run",
    )
    candidate_text = TASK.replace(
        "### Research basis",
        "### Decisions\nHuman — Marco: Use chicken.\n### Research basis",
    )
    candidate_path = write(tmp_path, "first-canonical-decision.txt", candidate_text)

    preflight = application.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=candidate_path,
    )
    assert preflight["code"] == "CONFIRMATION_REQUIRED"
    assert preflight["errors"][0]["rule"] == "decision_attestation_required"

    result = application.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"], file_path=candidate_path,
        governed_change_fields=["Decisions"],
    )
    assert result["ok"]
    attestation = application.conn.execute(
        """SELECT actor_agent,actor_provenance,details,before_state,after_state
             FROM audit_events
            WHERE operation_id=? AND event_type='decision.agent_attested'
            ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (started["submission_id"],),
    ).fetchone()
    assert attestation["actor_agent"] == "gpt"
    assert json.loads(attestation["actor_provenance"])["run_id"] == "research-run"
    assert json.loads(attestation["before_state"])["Decisions"] == []
    assert json.loads(attestation["after_state"])["Decisions"] == [
        "Human — Marco: Use chicken."
    ]


@pytest.mark.smoke
def test_initial_prepare_decision_attestation_rejected_without_matching_append(tmp_path):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start", agent="gpt", task_gid="t", kind="initial",
        change_level=None, change_reason=None, run_id="research-run",
    )

    result = application.execute(
        "prepare", agent="gpt", model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_path=write(tmp_path, "unchanged.txt", TASK),
        governed_change_fields=["Decisions"],
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "decision_attestation_not_applicable"
    assert backend.writes == 0

@pytest.mark.smoke
def test_stale_baseline_blocks_before_write(tmp_path):
    lines=TASK.splitlines(); b=Backend(lines[0],"\n".join(lines[1:])+"\n"); a=app(tmp_path,b)
    started=a.execute("start",agent="gpt",task_gid="t",kind="initial",change_level=None,change_reason=None)
    b.title = b.title + " changed"
    result=a.execute("prepare", model="gpt-5.6-sol",agent="gpt",submission_id=started["submission_id"],file_path=write(tmp_path,"c.txt",TASK))
    assert result["code"] == "CONFLICT" and b.writes == 0 and b.moves == 0
@pytest.mark.smoke
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
            a = app(case, b)
        elif kind == "change":
            from tests.support.readiness import _approve_and_submit
            from tests.support.verification import make_app

            a, b, signed_operation_id, _ = make_app(case)
            _approve_and_submit(a, signed_operation_id)
            candidate_text = TASK
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
            expected_section_gid=b.section,
            schema_version="2",
            actors=actors,
        )
        writes_before = b.writes
        moves_before = b.moves
        b.section = "drifted-section"
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
        assert b.writes == writes_before
        assert b.moves == moves_before
@pytest.mark.smoke
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
    import shlex
    argv = shlex.split(blocked["data"]["admin_command"])
    assert argv[:3] == ["dish-admin", "reopen-planning", "t"]
    assert argv[argv.index("--reason") + 1] == (
        "<why this completed task must be reopened>"
    )
    assert blocked["data"]["admin_command_is_template"] is True
    assert blocked["data"]["legal_next_step"] == (
        "Marco/admin runs reopen-planning with a reason; after it succeeds, "
        "retry start with kind=planning using a fresh client.request_id"
    )
    directive = blocked["data"]["directive"]
    assert blocked["data"]["admin_command"] in directive
    assert "replacing the placeholder text" in directive
    assert "retry start with kind=planning" in directive
    assert "Do not create a replacement operation" in directive
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
@pytest.mark.smoke
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
