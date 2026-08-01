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
@pytest.mark.parametrize("submission_id", ["not-an-operation", "", " "])
def test_action_rejects_malformed_submission_id_before_database_routing(
    tmp_path, submission_id,
    running_server,
):
    _backend, server, thread, url = running_server()
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.execute(
        "inspect", agent="gpt", submission_id=submission_id
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert result["errors"] == [
        {"expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "field": "submission_id", "rule": "uuid_identifier_required"}
    ]
@pytest.mark.smoke
def test_action_rejects_malformed_lease_operation_id(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.renew_lease("not-an-operation")
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "field": "operation_id", "rule": "uuid_identifier_required"}
    ]
@pytest.mark.smoke
@pytest.mark.parametrize(
    ("field", "value", "rule"),
    [
        ("project_gid", "project", "numeric_identifier_required"),
        ("section_gid", "-123", "numeric_identifier_required"),
        ("operation_id", "operation", "uuid_identifier_required"),
        ("cycle_id", "cycle", "uuid_identifier_required"),
        ("verification_cycle_id", "cycle", "uuid_identifier_required"),
    ],
)
def test_all_http_identifier_field_classes_use_strict_grammar(field, value, rule):
    with pytest.raises(DishRuleError) as caught:
        validate_identifier_fields({field: value})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.rule == rule
    expected = {"field": field}
    if rule == "uuid_identifier_required":
        expected["expected_format"] = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    assert caught.value.details == expected
@pytest.mark.smoke
def test_action_sanitizes_raw_backend_rejection(tmp_path, running_server):
    backend, server, thread, url = running_server()

    def rejected(_gid):
        raise BackendFailure(
            "BACKEND_REJECTED",
            "Asana API error (400) https://app.asana.com/private raw-body",
            status=400,
            retryable=True,
        )

    backend.read_task = rejected
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.execute("read", agent="gpt", task_gid="123456789")
    rendered = json.dumps(result)
    assert result["code"] == "BACKEND_REJECTED"
    assert result["data"]["message"] == "backend request was rejected"
    assert "asana.com" not in rendered.lower()
    assert "raw-body" not in rendered
@pytest.mark.smoke
def test_inaccessible_backend_identifier_is_distinct_and_non_retryable():
    class Forbidden(Exception):
        status = 403
        body = "private access policy detail"
        reason = "Forbidden"

    failure = map_backend_exception(
        Forbidden(),
        phase=RequestPhase.RESPONSE_RECEIVED,
        context="task 123456789",
    )
    result = error_envelope("read", failure, task_gid="123456789")

    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is False
    assert result["errors"] == [{"rule": "backend_access_denied"}]
    assert result["data"]["message"] == "backend request was rejected"
    assert "private access policy" not in json.dumps(result)
@pytest.mark.smoke
def test_canonical_operation_uuid_is_accepted_by_boundary_validator():
    operation_id = str(uuid.uuid4())
    assert validate_identifier_fields({"submission_id": operation_id}) is None
@pytest.mark.smoke
def test_uuid_validation_message_names_field_and_expected_format(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    action = DishActionClient(url, token="action-secret", run_id="sections-001")
    result = action.execute("sections", agent="gpt")
    assert result["errors"][0] == {
        "expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "field": "client.run_id",
        "rule": "uuid_identifier_required",
    }
    assert (
        "non-nil canonical lowercase UUID in 8-4-4-4-12 form"
        in result["data"]["message"]
    )
@pytest.mark.smoke
def test_action_request_limit_applies_before_workflow_execution(tmp_path, running_server):
    backend, server, thread, url = running_server(max_body=80)
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    result = action.execute("create", agent="gpt", title="x" * 500)
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["rule"] == "request_too_large"
    assert backend.writes == 0
@pytest.mark.smoke
def test_pre_body_auth_and_size_rejections_close_the_connection(tmp_path, running_server):
    backend, server, thread, url = running_server(max_body=80)
    valid_body = json.dumps(
        {"client": {"run_id": "f946b9ec-2b97-5b20-9831-e749d02e9883"}, "arguments": {"agent": "gpt"}}
    )
    oversized_body = json.dumps(
        {"client": {"run_id": "f946b9ec-2b97-5b20-9831-e749d02e9883"}, "arguments": {"title": "x" * 500}}
    )
    rejected_auth = _raw_post(
        url, "/v1/action/sections", token="cli-secret", body=valid_body
    )
    rejected_size = _raw_post(
        url, "/v1/action/create", token="action-secret", body=oversized_body
    )
    assert rejected_auth[0] == 403
    assert rejected_size[0] == 200
    for _status, connection_header, will_close, _payload in (
        rejected_auth,
        rejected_size,
    ):
        assert connection_header == "close"
        assert will_close
    assert rejected_auth[3]["errors"][0]["rule"] == "service_scope_forbidden"
    assert rejected_size[3]["errors"][0]["rule"] == "request_too_large"
    assert backend.writes == 0
@pytest.mark.smoke
def test_failed_start_request_id_cannot_be_reused_for_different_work(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    request_id = str(uuid.uuid4())
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    first = action.execute(
        "start", agent="gpt", task_gid="not-a-gid", kind="planning",
        request_id=request_id,
    )
    second = action.execute(
        "start", agent="gpt", task_gid="123456789", kind="initial",
        request_id=request_id,
    )
    assert first["code"] == "INVALID_ARGUMENT"
    assert first["data"]["request_id"] == request_id
    assert second["code"] == "CONFLICT"
    assert second["errors"][0]["rule"] == "service_request_identity_conflict"
@pytest.mark.smoke
def test_failed_start_request_replays_stored_validation_result(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    request_id = str(uuid.uuid4())
    action = DishActionClient(url, token="action-secret", run_id="f946b9ec-2b97-5b20-9831-e749d02e9883")
    first = action.execute(
        "start", agent="gpt", task_gid="not-a-gid", kind="planning",
        request_id=request_id,
    )
    second = action.execute(
        "start", agent="gpt", task_gid="not-a-gid", kind="planning",
        request_id=request_id,
    )
    assert first["code"] == "INVALID_ARGUMENT"
    assert second["code"] == "INVALID_ARGUMENT"
    assert second["data"]["request_replayed"] is True
    assert second["data"]["request_id"] == request_id
@pytest.mark.smoke
@pytest.mark.parametrize("task_gid", ["0", "0000", "0123456789"])
def test_action_rejects_noncanonical_numeric_task_gid_before_backend_call(tmp_path, task_gid, running_server):
    backend, server, thread, url = running_server()
    action = DishActionClient(
        url,
        token="action-secret",
        run_id="7ee726a0-06c2-4d12-a2b6-0c206d64c7e5",
    )
    result = action.execute(
        "start",
        request_id=str(uuid.uuid4()),
        agent="gpt",
        kind="planning",
        task_gid=task_gid,
    )
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["field"] == "task_gid"
    assert result["errors"][0]["rule"] == "numeric_identifier_required"
@pytest.mark.smoke
def test_action_rejects_noncanonical_client_run_id_before_work(tmp_path, running_server):
    backend, server, thread, url = running_server()
    action = DishActionClient(url, token="action-secret", run_id="not-a-run")
    result = action.execute("sections", agent="gpt")
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"][0]["field"] == "client.run_id"
    assert result["errors"][0]["rule"] == "uuid_identifier_required"
@pytest.mark.smoke
def test_action_lease_renewal_rejects_legacy_path_and_top_level_operation_id(tmp_path, running_server):
    _backend, server, thread, url = running_server()
    operation_id = "99999999-9999-4999-8999-999999999999"
    run_id = "f946b9ec-2b97-5b20-9831-e749d02e9883"
    request_id = str(uuid.uuid4())
    legacy = _raw_post(
        url,
        f"/v1/action/leases/{operation_id}/renew",
        token="action-secret",
        body=json.dumps(
            {"client": {"run_id": run_id, "request_id": request_id}}
        ),
    )
    top_level = _raw_post(
        url,
        "/v1/action/renew-lease",
        token="action-secret",
        body=json.dumps(
            {
                "client": {"run_id": run_id, "request_id": request_id},
                "operation_id": operation_id,
            }
        ),
    )
    assert legacy[0] == 404
    assert legacy[3] == {"ok": False, "error": "not_found"}
    assert top_level[0] == 200
    assert top_level[3]["code"] == "INVALID_ARGUMENT"
    assert top_level[3]["errors"] == [
        {"field": "operation_id", "rule": "request_field_unexpected"}
    ]
