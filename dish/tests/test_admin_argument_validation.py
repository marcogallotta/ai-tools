from __future__ import annotations

import uuid

import pytest

from dish_tool.admin import DishAdminApplication
from dish_tool.admin_cli import build_parser
from tests.support.service_scenarios import RUN_ID, post as _post, running as _running
from tests.support.thread_teardown import join_thread, stop_server
from tests.support.submission import _signed


@pytest.mark.parametrize(
    ("command", "required_field"),
    [
        ("migrate", "task_gid"),
        ("reopen-planning", "task_gid"),
        ("reopen", "submission_id"),
        ("recover", "submission_id"),
        ("repair-destination", "submission_id"),
        ("supply-evidence", "submission_id"),
        ("record-human-decision", "submission_id"),
        ("resolved", "submission_id"),
        ("authorize-governed-change", "submission_id"),
        ("discard", "submission_id"),
    ],
)
def test_empty_generic_admin_arguments_are_structured_and_replayable(
    tmp_path, command, required_field
):
    _service, backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    payload = {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": {},
    }
    try:
        first_status, first = _post(
            url,
            f"/v1/admin/{command}",
            token="admin-secret",
            payload=payload,
        )
        replay_status, replay = _post(
            url,
            f"/v1/admin/{command}",
            token="admin-secret",
            payload=payload,
        )
    finally:
        stop_server(server, thread)

    assert first_status == replay_status == 200
    assert first["ok"] is False
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["errors"] == [
        {"field": required_field, "rule": "argument_required"}
    ]
    assert first["data"]["request_id"] == request_id
    assert "request_replayed" not in first["data"]

    assert replay["ok"] is False
    assert replay["code"] == "INVALID_ARGUMENT"
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_id"] == request_id
    assert replay["data"]["request_replayed"] is True
    assert backend.writes == 0


@pytest.mark.parametrize(
    ("arguments", "required_field"),
    [
        ({"submission_id": str(uuid.uuid4()), "reason": "live reread"}, "outcome"),
        ({"submission_id": str(uuid.uuid4()), "outcome": "applied"}, "reason"),
    ],
)
def test_recover_validates_required_fields_before_unknown_operation_and_replays(
    tmp_path, arguments, required_field
):
    _service, backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    payload = {
        "client": {"run_id": RUN_ID, "request_id": request_id},
        "arguments": arguments,
    }
    try:
        first_status, first = _post(
            url,
            "/v1/admin/recover",
            token="admin-secret",
            payload=payload,
        )
        replay_status, replay = _post(
            url,
            "/v1/admin/recover",
            token="admin-secret",
            payload=payload,
        )
    finally:
        stop_server(server, thread)

    assert first_status == replay_status == 200
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["errors"] == [
        {"field": required_field, "rule": "argument_required"}
    ]
    assert replay["code"] == "INVALID_ARGUMENT"
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_replayed"] is True
    assert backend.writes == 0


@pytest.mark.parametrize(
    ("arguments", "field", "rule"),
    [
        (
            {"submission_id": "terminal", "outcome": " ", "reason": "live reread"},
            "outcome",
            "recovery_outcome_required",
        ),
        (
            {"submission_id": "terminal", "outcome": "applied", "reason": " "},
            "reason",
            "recovery_reason_required",
        ),
    ],
)
def test_recover_validates_blank_fields_before_terminal_operation(
    tmp_path, arguments, field, rule
):

    application, backend, operation_id = _signed(tmp_path)
    submitted = application.execute("submit", submission_id=operation_id)
    assert submitted["ok"]

    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    result = admin.execute(
        "recover",
        **{**arguments, "submission_id": operation_id},
    )

    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [{"field": field, "rule": rule}]


def test_record_human_decision_help_discloses_governed_field_boundary(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["record-human-decision", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "does not modify governed fields" in help_text
    assert "authorize-governed-change" in help_text

    with pytest.raises(SystemExit):
        parser.parse_args(["record-human-decision", "-h"])
    detail_help_text = " ".join(capsys.readouterr().out.split())
    assert "does not itself change Exemptions, Locks, or other canonical fields" in detail_help_text


def test_supply_evidence_help_stays_route_specific(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["supply-evidence", "--help"])
    help_text = capsys.readouterr().out
    assert "governed" not in help_text


def test_recover_parser_accepts_generated_inspect_outcome():
    operation_id = str(uuid.uuid4())
    parsed = build_parser().parse_args(
        [
            "recover",
            operation_id,
            "--outcome",
            "inspect",
            "--reason",
            "fresh live reread required",
        ]
    )
    assert parsed.outcome == "inspect"


def test_admin_inspect_is_a_first_class_human_command():
    operation_id = str(uuid.uuid4())
    parsed = build_parser().parse_args(["inspect", operation_id])
    assert parsed.command == "inspect"
    assert parsed.submission_id == operation_id


def test_admin_attention_is_a_first_class_read_only_command():
    parsed = build_parser().parse_args(["attention"])
    assert parsed.command == "attention"


def test_human_renderer_surfaces_recovery_actions_from_errors():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": False,
        "command": "authorize-governed-change",
        "code": "VALIDATION_FAILED",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": True,
        "allowed_actions": [],
        "data": {"message": "authorization required"},
        "errors": [
            {
                "rule": "governed_change_unauthorized",
                "human_action": {
                    "kind": "authorize-governed-change",
                    "summary": "Authorize the exact Exemptions change.",
                    "effect": "Create one authorization without editing the task.",
                    "shell_command": "dish-admin authorize-governed-change operation-1 --field Exemptions",
                },
            }
        ],
    }
    rendered = render_admin_result(result, profile="prod")
    assert "Could not authorize-governed-change" in rendered
    assert "Authorize the exact Exemptions change." in rendered
    assert "dish-admin authorize-governed-change operation-1" in rendered
    assert '"errors"' not in rendered


def test_human_renderer_explains_authorization_success_without_claiming_a_write():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "authorize-governed-change",
        "code": "OK",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": False,
        "allowed_actions": [],
        "data": {"field": "Exemptions"},
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Authorization recorded" in rendered
    assert "task itself was not changed" in rendered
    assert "retry the same exact candidate" in rendered


def _parse_generated_human_action(action):
    import re
    import shlex

    argv = shlex.split(action["shell_command"])
    assert argv[0] == "dish-admin"
    filled = [
        re.sub(r"<[^>]+>", "operator supplied reason", token)
        for token in argv[1:]
    ]
    return build_parser().parse_args(filled)


def test_governed_exemptions_action_explains_approval_and_roundtrips_parser():
    from dish_tool.human_actions import governed_change_action, relay_text

    after = (
        "[nutrition-kcal] Scope: this tasting may remain below 700 kcal. | "
        "[nutrition-protein] Scope: this tasting may remain below 35g protein."
    )
    spec = governed_change_action(
        operation_id=str(uuid.uuid4()),
        field="Exemptions",
        before="None",
        after=after,
    )
    action = spec.payload()["human_action"]

    parsed = _parse_generated_human_action(action)
    assert parsed.command == "authorize-governed-change"
    assert parsed.field == "Exemptions"
    assert parsed.before == "None"
    assert parsed.after == after
    assert action["context"]["governed_change"]["added_tokens"] == [
        "nutrition-kcal",
        "nutrition-protein",
    ]
    details = "\n".join(action["details"])
    assert "700–1,000 kcal" in details
    assert "minimum 35 g protein" in details
    assert "this task, this operation" in details
    assert "does not edit the task or approve Verification" in details
    assert "retry the same unchanged candidate" in details

    relay = relay_text(spec, instruction="Wait for Marco, then retry.")
    assert relay.index("Before the command") < relay.index("dish-admin authorize-governed-change")
    assert "[nutrition-kcal]" in relay
    assert "[nutrition-protein]" in relay


def test_human_renderer_shows_governed_change_details_before_command():
    from dish_tool.admin_human import render_admin_result
    from dish_tool.human_actions import governed_change_action

    spec = governed_change_action(
        operation_id="operation-1",
        field="Exemptions",
        before="None",
        after="[nutrition-kcal] controlled tasting",
    )
    result = {
        "ok": False,
        "command": "reject",
        "code": "VALIDATION_FAILED",
        "task_gid": "121",
        "submission_id": "operation-1",
        "state": "open",
        "retryable": True,
        "allowed_actions": [],
        "data": {"message": "authorization required"},
        "errors": [{"rule": "governed_change_unauthorized", **spec.payload()}],
    }

    rendered = render_admin_result(result, profile="prod")
    assert rendered.index("Change this task's Exemptions") < rendered.index(
        "Run: dish-admin authorize-governed-change"
    )
    assert "Scope: this task, this operation" in rendered
    assert "does not edit the task or approve Verification" in rendered


def test_admin_help_distinguishes_lease_recovery_expiry_and_abandonment(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    root_help = " ".join(capsys.readouterr().out.split())
    assert "Start with `dish-admin inspect TASK_OR_OPERATION`" in root_help
    assert "recover-lease lets the same durable agent run continue" in root_help
    assert "expire-lease only releases an active lease" in root_help
    assert "abandon-operation is for a run that will not return" in root_help

    with pytest.raises(SystemExit):
        parser.parse_args(["authorize-governed-change", "--help"])
    auth_help = " ".join(capsys.readouterr().out.split())
    assert "does not edit the task" in auth_help
    assert "approve Verification" in auth_help


def test_representative_generated_admin_commands_roundtrip_the_real_parser():
    import re
    import shlex

    from dish_tool.human_actions import exact_action, template_action, PromptField

    operation_id = str(uuid.uuid4())
    cycle_id = str(uuid.uuid4())
    lease_id = str(uuid.uuid4())
    abandonment_id = str(uuid.uuid4())
    task_gid = "1217091891356716"
    specs = [
        exact_action(
            kind="inspect-admin-state", command="inspect", positional=(operation_id,),
            summary="Inspect.", effect="Read only.",
        ),
        template_action(
            kind="reconcile-uncertain-effect", command="recover", positional=(operation_id,),
            options=(("--outcome", "<outcome>"), ("--reason", "<reason>")),
            prompt_fields=(PromptField("outcome", "Outcome", "<outcome>"), PromptField("reason", "Reason", "<reason>")),
            summary="Recover.", effect="Reconcile.",
        ),
        template_action(
            kind="repair-destination", command="repair-destination", positional=(operation_id,),
            options=(("--destination-section-gid", "<section>"), ("--reason", "<reason>")),
            prompt_fields=(PromptField("section", "Section", "<section>"), PromptField("reason", "Reason", "<reason>")),
            summary="Repair.", effect="Repair destination.",
        ),
        template_action(
            kind="abandon-dead-run", command="abandon-operation", positional=(operation_id,),
            options=(("--lease-id", lease_id), ("--reason", "<reason>")),
            prompt_fields=(PromptField("reason", "Reason", "<reason>"),),
            summary="Abandon.", effect="Prepare successor.",
        ),
        exact_action(
            kind="reconcile-abandonment", command="reconcile-abandonment", positional=(abandonment_id,),
            summary="Reconcile.", effect="Continue abandonment.",
        ),
        template_action(
            kind="reopen-planning", command="reopen-planning", positional=(task_gid,),
            options=(("--reason", "<reason>"),),
            prompt_fields=(PromptField("reason", "Reason", "<reason>"),),
            summary="Reopen.", effect="Reopen Planning.",
        ),
        template_action(
            kind="supply-evidence", command="supply-evidence", positional=(operation_id,),
            options=(("--detail", "<detail>"), ("--resume-status", "pending-verification"),
                     ("--expected-task-gid", task_gid), ("--expected-cycle-id", cycle_id),
                     ("--expected-hold-identity", "identity")),
            prompt_fields=(PromptField("detail", "Detail", "<detail>"),),
            summary="Evidence.", effect="Release hold.",
        ),
        template_action(
            kind="record-human-decision", command="record-human-decision", positional=(operation_id,),
            options=(("--detail", "<detail>"), ("--resume-status", "pending-verification"),
                     ("--expected-task-gid", task_gid), ("--expected-cycle-id", cycle_id),
                     ("--expected-hold-identity", "identity")),
            prompt_fields=(PromptField("detail", "Detail", "<detail>"),),
            summary="Decision.", effect="Release hold.",
        ),
        exact_action(
            kind="resolved", command="resolved", positional=(operation_id,),
            summary="Resolve.", effect="Release unchanged.",
        ),
        template_action(
            kind="recover-lease", command="recover-lease", positional=(operation_id,),
            options=(("--reason", "<reason>"),),
            prompt_fields=(PromptField("reason", "Reason", "<reason>"),),
            summary="Recover lease.", effect="Same run resumes.",
        ),
        template_action(
            kind="expire-lease", command="expire-lease", positional=(lease_id,),
            options=(("--reason", "<reason>"),),
            prompt_fields=(PromptField("reason", "Reason", "<reason>"),),
            summary="Expire lease.", effect="Release lease.",
        ),
    ]

    replacements = {
        "<outcome>": "inspect",
        "<reason>": "operator supplied reason",
        "<section>": "1217091890481531",
        "<detail>": "operator supplied detail",
    }
    for spec in specs:
        argv = shlex.split(spec.shell_command())
        filled = [replacements.get(token, re.sub(r"<[^>]+>", "operator supplied", token)) for token in argv[1:]]
        parsed = build_parser().parse_args(filled)
        assert parsed.command == spec.command


def test_output_flags_are_accepted_before_or_after_admin_subcommand():
    from dish_tool.admin_cli import _normalize_output_flags

    operation_id = str(uuid.uuid4())
    before = _normalize_output_flags(["--json", "--verbose", "inspect", operation_id])
    after = _normalize_output_flags(["inspect", operation_id, "--verbose", "--json"])
    assert build_parser().parse_args(before).command == "inspect"
    parsed_after = build_parser().parse_args(after)
    assert parsed_after.command == "inspect"
    assert parsed_after.json is True
    assert parsed_after.verbose is True


def test_human_renderer_summarizes_global_attention_items():
    from dish_tool.admin_human import render_admin_result

    result = {
        "ok": True,
        "command": "attention",
        "code": "OK",
        "state": "ok",
        "retryable": False,
        "allowed_actions": [],
        "data": {
            "checked_count": 3,
            "attention_count": 1,
            "healthy_count": 2,
            "category_counts": {
                "safe_cleanup": 0,
                "multi_step_safe": 1,
                "needs_marco": 0,
                "unsafe": 0,
            },
            "attention_items": [
                {
                    "category": "multi_step_safe",
                    "task_title": "Laap gai",
                    "operation_id": "operation-1",
                    "problem": "A dead verifier attempt must be abandoned.",
                    "human_actions": [
                        {
                            "summary": "Abandon the dead verifier attempt.",
                            "shell_command": "dish-admin abandon-operation operation-1 --lease-id lease-1",
                        }
                    ],
                }
            ],
        },
        "errors": [],
    }

    rendered = render_admin_result(result, profile="prod")

    assert "Dish attention" in rendered
    assert "Workflow records checked: 3" in rendered
    assert "[SAFE MULTI-STEP] Laap gai" in rendered
    assert "dish-admin abandon-operation operation-1" in rendered
