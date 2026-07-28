from __future__ import annotations

import json

from dish_tool import admin_cli, cli
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
