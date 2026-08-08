import json
import threading
import uuid
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from dish_service.action_guidance import action_agent_guidance
from dish_service.application import DishService
from dish_service.client import DishActionClient, DishAdminServiceClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.command_spec import validate_action_request
from dish_service.http import build_server
from dish_service.identifiers import validate_identifier_fields
from dish_tool.backend import map_backend_exception
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import RequestPhase
from dish_tool.results import error_envelope
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK
from tests.support.action_http import _raw_post, running_server






@pytest.mark.smoke
def test_action_adds_contextual_guidance_without_changing_canonical_sections_result(
    tmp_path, running_server
):
    _backend, server, thread, url = running_server()
    cli = DishServiceClient(url, token="cli-secret", run_id="9940d276-a582-5787-b6d9-b4fba846e271")
    action = DishActionClient(url, token="action-secret", run_id="7b87f6d2-db66-5199-882f-07841e94589c")
    cli_result = cli.execute("sections", agent="gpt")
    action_result = action.execute("sections", agent="gpt")

    guidance = action_result["data"].pop("agent_guidance")
    assert guidance["source"] == "dish"
    assert guidance["state_specific"] is True
    assert "Use only allowed_actions" in guidance["instructions"][0]
    assert cli_result == action_result
@pytest.mark.smoke
def test_action_credential_is_rejected_from_cli_and_admin_surfaces(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    wrong_cli = DishServiceClient(url, token="action-secret", run_id="7b87f6d2-db66-5199-882f-07841e94589c")
    wrong_admin = DishAdminServiceClient(url, token="action-secret", run_id="7b87f6d2-db66-5199-882f-07841e94589c")
    cli_result = wrong_cli.execute("read", agent="gpt", task_gid="t")
    admin_result = wrong_admin.execute("discard", submission_id="x", reason="x")
    assert cli_result["errors"][0]["rule"] == "service_scope_forbidden"
    assert admin_result["errors"][0]["rule"] == "service_scope_forbidden"
@pytest.mark.smoke
def test_cli_credential_is_rejected_from_action_surface(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    wrong = DishActionClient(url, token="cli-secret", run_id="9940d276-a582-5787-b6d9-b4fba846e271")
    result = wrong.execute("sections", agent="gpt")
    assert result["code"] == "AGENT_MISMATCH"
    assert result["errors"][0]["rule"] == "service_scope_forbidden"
@pytest.mark.smoke
def test_action_surface_supports_leased_start_prepare_and_heartbeat(tmp_path, running_server):
    backend, server, thread, url = running_server()
    action = DishActionClient(url, token="action-secret", run_id="60f24aac-64a6-590a-99f5-a52fb9aae0a5")
    started = action.execute(
        "start", agent="gpt", task_gid="123456789", kind="initial"
    )
    renewed = action.renew_lease(started["submission_id"])
    prepared = action.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=started["submission_id"],
        file_text=TASK,
    )
    inspected = action.execute(
        "inspect", agent="gpt", submission_id=started["submission_id"],
    )
    assert started["ok"]
    assert renewed["ok"]
    assert prepared["ok"]
    assert inspected["data"]["operation"]["run_id"] == "60f24aac-64a6-590a-99f5-a52fb9aae0a5"
    assert inspected["data"]["actors"]["run_id"] == "60f24aac-64a6-590a-99f5-a52fb9aae0a5"
    assert backend.writes == 1
    assert backend.moves == 1
@pytest.mark.smoke
@pytest.mark.parametrize(
    "task_gid",
    ["not-a-gid", "123abc", "", " ", "-1"],
)
def test_action_rejects_malformed_task_gid_before_backend_call(tmp_path, task_gid, running_server):
    backend, server, thread, url = running_server()
    calls = 0
    original = backend.read_task

    def counted_read(gid):
        nonlocal calls
        calls += 1
        return original(gid)

    backend.read_task = counted_read
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.execute("read", agent="gpt", task_gid=task_gid)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {"field": "task_gid", "rule": "numeric_identifier_required"}
    ]
    assert calls == 0
@pytest.mark.smoke
def test_action_distinguishes_nonexistent_numeric_gid_and_reaches_backend(tmp_path, running_server):
    backend, server, thread, url = running_server()
    calls = 0

    def missing(gid):
        nonlocal calls
        calls += 1
        raise BackendFailure(
            "BACKEND_REJECTED",
            "private Asana 404 response body",
            status=404,
            retryable=True,
        )

    backend.read_task = missing
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.execute("read", agent="gpt", task_gid="999999999")
    assert result["code"] == "NOT_FOUND"
    assert result["retryable"] is False
    assert result["errors"][0]["rule"] == "task_not_found"
    assert "private Asana" not in json.dumps(result)
    assert calls == 1
@pytest.mark.smoke
def test_valid_numeric_gid_reaches_action_backend_path(tmp_path, running_server):
    backend, server, thread, url = running_server()
    calls = 0
    original = backend.read_task

    def counted_read(gid):
        nonlocal calls
        calls += 1
        return original(gid)

    backend.read_task = counted_read
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.execute("read", agent="gpt", task_gid="123456789")
    assert result["ok"]
    assert calls == 2
@pytest.mark.smoke
@pytest.mark.parametrize("command", ["read", "start"])
@pytest.mark.parametrize(
    "task_gid",
    ["9223372036854775808", "99999999999999999999"],
)
def test_action_rejects_out_of_range_task_gid_before_backend_call(
    tmp_path, command, task_gid,
    running_server,
):
    backend, server, thread, url = running_server()
    calls = 0
    original = backend.read_task

    def counted_read(gid):
        nonlocal calls
        calls += 1
        return original(gid)

    backend.read_task = counted_read
    arguments = {"agent": "gpt", "task_gid": task_gid}
    if command == "start":
        arguments["kind"] = "planning"
    action = DishActionClient(
        url,
        token="action-secret",
        run_id="f946b9ec-2b97-5b20-9831-e749d02e9883",
    )
    result = action.execute(command, **arguments)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {
            "expected_format": "decimal integer from 1 to 9223372036854775807",
            "field": "task_gid",
            "rule": "numeric_identifier_out_of_range",
        }
    ]
    assert calls == 0
@pytest.mark.smoke
def test_action_accepts_maximum_supported_task_gid(tmp_path, running_server):
    backend, server, thread, url = running_server()
    calls = 0

    def missing(gid):
        nonlocal calls
        calls += 1
        assert gid == "9223372036854775807"
        raise BackendFailure(
            "BACKEND_REJECTED",
            "private Asana 404 response body",
            status=404,
            retryable=True,
        )

    backend.read_task = missing
    action = DishActionClient(
        url,
        token="action-secret",
        run_id="f946b9ec-2b97-5b20-9831-e749d02e9883",
    )
    result = action.execute(
        "read", agent="gpt", task_gid="9223372036854775807"
    )
    assert result["code"] == "NOT_FOUND"
    assert calls == 1



@pytest.mark.smoke
@pytest.mark.parametrize("advertised_command", ["prepare", "reject"])
def test_connected_advertised_workflow_actions_are_callable(
    tmp_path, running_server, advertised_command
):
    _backend, _server, _thread, url = running_server()
    action = DishActionClient(
        url,
        token="action-secret",
        run_id="d46d9810-1fb8-4ad5-bf65-8336a9a7e1ba",
    )
    started = action.execute(
        "start", agent="gpt", task_gid="123456789", kind="initial"
    )
    inspected = action.execute(
        "inspect", agent="gpt", submission_id=started["submission_id"]
    )

    assert inspected["allowed_actions"] == ["prepare", "reject"]
    assert inspected["allowed_actions"] == inspected["data"]["authoritative_view"]["legal_actions"]
    assert advertised_command in inspected["allowed_actions"]

    if advertised_command == "prepare":
        result = action.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=started["submission_id"],
            file_text=TASK,
        )
    else:
        result = action.execute(
            "reject",
            agent="gpt",
            submission_id=started["submission_id"],
            reason="Human review is required before construction.",
            route="human-review",
            resume_status="pending-research",
            human_review_confirmed=True,
            human_review_basis="Only Marco can resolve the remaining choice within settled authority.",
            repairs_considered="Plausible within-authority repairs were considered and do not resolve the choice.",
        )

    assert result["ok"] is True



@pytest.mark.smoke
def test_connected_action_can_execute_advertised_safe_reclaim(tmp_path, running_server):
    _backend, _server, _thread, url = running_server()
    old = DishActionClient(
        url,
        token="action-secret",
        run_id="9b85f42f-94f7-4c89-9e58-660fa8502a85",
    )
    started = old.execute(
        "start", agent="gpt", task_gid="123456789", kind="initial"
    )
    assert started["ok"] is True

    from dish_tool.database import initialize_database

    conn = initialize_database(tmp_path / "shared.db")
    try:
        lease = conn.execute(
            "SELECT lease_id FROM service_leases WHERE operation_id=? AND lease_kind='actor' ORDER BY actor_attempt_seq DESC LIMIT 1",
            (started["submission_id"],),
        ).fetchone()
        assert lease is not None
        lease_id = lease["lease_id"]
    finally:
        conn.close()

    admin = DishAdminServiceClient(
        url,
        token="admin-secret",
        run_id="c99033ef-f29c-4a65-80fc-440d523ee9a3",
    )
    expired = admin.expire_lease(
        lease_id=lease_id,
        reason="test old run is gone",
        request_id=str(uuid.uuid4()),
    )
    assert expired["ok"] is True

    fresh = DishActionClient(
        url,
        token="action-secret",
        run_id="cd05b35a-f384-4ca7-946a-f3b8b3400e91",
    )
    discovered = fresh.execute("read", agent="gpt", task_gid="123456789")
    assert discovered["allowed_actions"] == ["safe-reclaim"]
    action = discovered["data"]["agent_action"]
    assert action == {
        "command": "safe-reclaim",
        "arguments": {
            "submission_id": started["submission_id"],
            "lease_id": lease_id,
            "agent": "gpt",
        },
    }

    reclaimed = fresh.execute("safe-reclaim", **action["arguments"])
    assert reclaimed["ok"] is True
    assert reclaimed["state"] == "reclaimed"
    assert reclaimed["allowed_actions"] == ["start"]


def test_service_refuses_to_advertise_non_callable_connected_action():
    result = {"ok": True, "allowed_actions": [], "data": {}}
    with pytest.raises(DishRuleError) as exc:
        DishService._synchronize_exposed_actions(result, ["not-a-real-action"])
    assert exc.value.rule == "allowed_action_surface_mismatch"
    assert "not-a-real-action" in exc.value.details["unsupported_actions"]
    assert "apply-proposal" in exc.value.details["callable_actions"]


def test_action_guidance_renders_state_specific_continuations():
    result = {
        "ok": True,
        "command": "approve",
        "code": "OK",
        "allowed_actions": ["submit"],
        "data": {"batch_may_continue": True},
    }

    guidance = action_agent_guidance(result)

    assert "Call submit in this same run." in guidance["instructions"]
    assert any("safely parked" in item for item in guidance["instructions"])


def test_action_guidance_fails_closed_on_backend_uncertainty():
    guidance = action_agent_guidance({
        "ok": False,
        "command": "prepare",
        "code": "BACKEND_UNCERTAIN",
        "allowed_actions": [],
        "data": {},
    })

    assert any("Stop." in item and "new request ID" in item for item in guidance["instructions"])


def test_action_guidance_points_to_exact_returned_agent_action():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "read",
        "code": "OK",
        "allowed_actions": ["safe-reclaim"],
        "data": {
            "agent_action": {
                "command": "safe-reclaim",
                "arguments": {
                    "submission_id": "11111111-1111-4111-8111-111111111111",
                    "lease_id": "22222222-2222-4222-8222-222222222222",
                },
            }
        },
    })
    assert any(
        instruction.startswith("Call safe-reclaim with the target arguments in data.agent_action.arguments exactly")
        and "add only caller/request fields required by the current Action schema" in instruction
        for instruction in guidance["instructions"]
    )


def test_missing_inspect_request_id_explains_possible_stale_action_schema():
    with pytest.raises(DishRuleError) as exc:
        validate_action_request(
            "inspect",
            {
                "client": {"run_id": "11111111-1111-4111-8111-111111111111"},
                "arguments": {
                    "agent": "gpt",
                    "submission_id": "22222222-2222-4222-8222-222222222222",
                },
            },
        )
    assert exc.value.rule == "request_field_required"
    assert "records durable Verification evidence" in str(exc.value)
    assert "refresh or re-import the Dish Action schema" in str(exc.value)
