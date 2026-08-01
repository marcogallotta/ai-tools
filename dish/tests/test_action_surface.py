import json
import threading
import uuid
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.client import DishActionClient, DishAdminServiceClient, DishServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.identifiers import validate_identifier_fields
from dish_tool.backend import map_backend_exception
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import RequestPhase
from dish_tool.results import error_envelope
from tests.support.service_foundation import _release_loader
from tests.support.verification import Backend, TASK
from tests.support.action_http import (
    _raw_post,
    _running,
    _stop,

)






@pytest.fixture
def running_server(tmp_path):
    active = []

    def start(*, max_body=2 * 1024 * 1024):
        running = _running(tmp_path, max_body=max_body)
        active.append((running[1], running[2]))
        return running

    yield start

    for server, thread in reversed(active):
        _stop(server, thread)

@pytest.mark.smoke
def test_cli_and_action_receive_identical_workflow_results(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    cli = DishServiceClient(url, token="cli-secret", run_id="9940d276-a582-5787-b6d9-b4fba846e271")
    action = DishActionClient(url, token="action-secret", run_id="7b87f6d2-db66-5199-882f-07841e94589c")
    cli_result = cli.execute("sections", agent="gpt")
    action_result = action.execute("sections", agent="gpt")
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
