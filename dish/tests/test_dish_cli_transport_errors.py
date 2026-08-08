from __future__ import annotations

import json

import pytest

from dish_service import admin_cli, cli
from dish_tool.errors import DishRuleError


class FailingAgentClient:
    def execute(self, _command, **_arguments):
        raise DishRuleError(
            "BACKEND_REJECTED",
            "dish service is unavailable",
            rule="service_unavailable",
            retryable=True,
        )


class CrashingAgentClient:
    def execute(self, _command, **_arguments):
        raise RuntimeError("transport exploded")


class FailingAdminClient:
    def create_backup(self, *, label):
        raise DishRuleError(
            "BACKEND_REJECTED",
            "dish service is unavailable",
            rule="service_unavailable",
            retryable=True,
        )


def _output(capsys):
    return json.loads(capsys.readouterr().out)


def test_agent_cli_renders_command_time_transport_error_as_json(capsys):
    status = cli.main(
        ["read", "123", "--agent", "gpt"],
        application=FailingAgentClient(),
    )

    result = _output(capsys)
    assert status != 0
    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is True
    assert result["errors"][0]["rule"] == "service_unavailable"
    assert result["task_gid"] == "123"


def test_agent_cli_renders_unexpected_command_failure_as_json(capsys):
    status = cli.main(
        ["read", "123", "--agent", "gpt"],
        application=CrashingAgentClient(),
    )

    result = _output(capsys)
    assert status != 0
    assert result["code"] == "INTERNAL_ERROR"
    assert result["errors"][0]["rule"] == "command_failure"
    assert result["errors"][0]["error_type"] == "RuntimeError"


def test_admin_cli_preserves_command_time_transport_error(capsys):
    status = admin_cli.main(
        ["backup-create", "--label", "before"],
        application=FailingAdminClient(),
    )

    result = _output(capsys)
    assert status != 0
    assert result["code"] == "BACKEND_REJECTED"
    assert result["errors"][0]["rule"] == "service_unavailable"


def test_admin_cli_preserves_structured_startup_error(monkeypatch, capsys):
    def fail_startup():
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "live mode requires the shared dish service",
            rule="shared_service_required",
        )

    monkeypatch.setattr(admin_cli, "build_application", fail_startup)
    status = admin_cli.main(["backup-create"])

    result = _output(capsys)
    assert status != 0
    assert result["code"] == "PROTOCOL_INCOMPATIBLE"
    assert result["errors"][0]["rule"] == "shared_service_required"


def test_submit_cli_no_longer_advertises_or_accepts_candidate_file():
    parser = cli.build_parser()
    try:
        parser.parse_args(["submit", "00000000-0000-4000-8000-000000000000", "--file", "ignored"])
    except DishRuleError as exc:
        assert exc.rule == "invalid_arguments"
    else:
        raise AssertionError("submit --file was accepted despite having no consumer")


def test_prepare_action_contract_rejects_removed_declarations():
    from dish_service.command_spec import action_argument_schema, validate_action_request

    schema = action_argument_schema("prepare")
    for field in (
        "exemption_revision",
        "dish_name",
        "recognition",
        "roles",
        "no_role_tags",
        "blockers",
        "no_blockers",
    ):
        assert field not in schema["properties"]

    request = {
        "client": {"run_id": "11111111-1111-4111-8111-111111111111", "request_id": "22222222-2222-4222-8222-222222222222"},
        "arguments": {
            "submission_id": "00000000-0000-4000-8000-000000000000",
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "file_text": "candidate",
            "dish_name": "ignored before this fix",
        },
    }
    try:
        validate_action_request("prepare", request)
    except DishRuleError as exc:
        assert exc.rule == "argument_unexpected"
        assert exc.details["field"] == "dish_name"
    else:
        raise AssertionError("removed declaration was accepted")


def test_reject_cli_rejects_removed_compatibility_flags():
    parser = cli.build_parser()
    for flag, value in (("--changed-since-prior", "identity"), ("--take-ownership", None)):
        argv = ["reject", "00000000-0000-4000-8000-000000000000", "--agent", "gpt", "--reason", "reason", "--route", "large", flag]
        if value is not None:
            argv.append(value)
        try:
            parser.parse_args(argv)
        except DishRuleError as exc:
            assert exc.rule == "invalid_arguments"
        else:
            raise AssertionError(f"{flag} was accepted despite having no consumer")


@pytest.mark.parametrize(
    ("argv", "field", "expected"),
    [
        (
            ["create", "--title", "Canonical", "--agent", "codex"],
            "agent",
            "codex",
        ),
        (
            ["start", "123", "--agent", "gpt", "--kind", "verification"],
            "kind",
            "verification",
        ),
        (
            [
                "start",
                "123",
                "--agent",
                "gpt",
                "--kind",
                "change",
                "--change-level",
                "large",
            ],
            "change_level",
            "large",
        ),
        (
            [
                "start",
                "123",
                "--agent",
                "gpt",
                "--kind",
                "planning",
                "--intent-basis",
                "agent_override",
            ],
            "intent_basis",
            "agent_override",
        ),
        (
            [
                "prepare",
                "00000000-0000-4000-8000-000000000000",
                "--agent",
                "gpt",
                "--model",
                "gpt-5.6-sol",
                "--file",
                "candidate.txt",
                "--material-classification",
                "non-material",
            ],
            "material_classification",
            "non-material",
        ),
        (
            [
                "approve",
                "00000000-0000-4000-8000-000000000000",
                "--agent",
                "gpt",
                "--model",
                "gpt-5.6-sol",
                "--correction",
                "small",
            ],
            "correction",
            "small",
        ),
        (
            [
                "reject",
                "00000000-0000-4000-8000-000000000000",
                "--agent",
                "gpt",
                "--reason",
                "reason",
                "--route",
                "human-review",
                "--resume-status",
                "pending-research",
            ],
            "route",
            "human-review",
        ),
    ],
)
def test_agent_cli_preserves_command_choice_behavior(argv, field, expected):
    parsed = cli.build_parser().parse_args(argv)

    assert getattr(parsed, field) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "--title", "Canonical", "--agent", "unknown"],
        ["start", "123", "--agent", "gpt", "--kind", "unknown"],
    ],
)
def test_agent_cli_rejects_values_outside_canonical_enums(argv):
    with pytest.raises(DishRuleError) as exc:
        cli.build_parser().parse_args(argv)

    assert exc.value.rule == "invalid_arguments"


class HumanAdminClient:
    def execute(self, command, **_arguments):
        assert command == "holds"
        return {
            "ok": True,
            "command": "holds",
            "code": "OK",
            "task_gid": None,
            "submission_id": None,
            "state": "ok",
            "retryable": False,
            "allowed_actions": [],
            "data": {"count": 0, "holds": []},
            "errors": [],
        }


def test_admin_cli_defaults_to_human_output_on_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli.sys.stdout, "isatty", lambda: True)

    status = admin_cli.main(["holds"], application=HumanAdminClient())

    output = capsys.readouterr().out
    assert status == 0
    assert "Environment: PROD" in output
    assert "Open holds: 0" in output
    assert not output.lstrip().startswith("{")


def test_admin_cli_json_flag_preserves_machine_envelope_on_a_terminal(
    monkeypatch, capsys
):
    monkeypatch.setattr(admin_cli.sys.stdout, "isatty", lambda: True)

    status = admin_cli.main(["holds", "--json"], application=HumanAdminClient())

    output = json.loads(capsys.readouterr().out)
    assert status == 0
    assert output["command"] == "holds"
    assert output["data"]["count"] == 0
