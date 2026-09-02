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
from dish_tool.identifiers import validate_identifier_fields
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
            human_review_options=[
                {"label": "Wait for Marco's chosen construction route", "decision": "Use Marco's chosen route before constructing the candidate."},
                {"label": "Return with another research option", "decision": "Research another plausible route before construction."},
            ],
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

    from dish_tool.database_initialization import initialize_database

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


def test_action_guidance_treats_read_identity_binding_as_canonical():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "read",
        "code": "OK",
        "allowed_actions": [],
        "data": {
            "identity_binding": {
                "dish_id": "91b697f6-8799-5c4d-b8ee-890b1386a644",
                "task_gid": "123456789",
            }
        },
    })
    text = " ".join(guidance["instructions"])
    assert "exact canonical Dish-to-task binding" in text
    assert "Use the returned task_gid for task-scoped continuation" in text
    assert "do not rediscover the Dish through sections or title matching" in text
    assert "never use dish_id as submission_id" in text


def test_action_guidance_uses_native_dish_id_when_read_has_no_task_alias():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "read",
        "code": "OK",
        "allowed_actions": ["start"],
        "data": {
            "identity_binding": {
                "dish_id": "91b697f6-8799-5c4d-b8ee-890b1386a644",
                "task_gid": None,
            }
        },
    })
    text = " ".join(guidance["instructions"])
    assert "No task_gid is bound; use the returned dish_id for task-scoped continuation" in text
    assert "never use dish_id as submission_id" in text


def test_action_guidance_redirects_dish_uuid_misuse_to_canonical_read():
    guidance = action_agent_guidance({
        "ok": False,
        "command": "inspect",
        "code": "NOT_FOUND",
        "allowed_actions": [],
        "data": {},
        "errors": [{"rule": "operation_not_found"}],
    })
    text = " ".join(guidance["instructions"])
    assert "submission_id is an operation/submission UUID, not a Dish UUID" in text
    assert "read(dish_id=<uuid>)" in text
    assert "rather than browsing sections or guessing" in text


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


def test_action_guidance_distinguishes_same_run_revival_from_fresh_safe_reclaim():
    same_run = action_agent_guidance(
        {
            "ok": True,
            "command": "read",
            "code": "OK",
            "allowed_actions": ["renew-lease"],
            "data": {
                "legal_next_step": (
                    "Resume this same durable run by renewing its expired actor lease; "
                    "do not create a new run_id or require Marco/admin recovery."
                ),
                "agent_action": {
                    "command": "renew-lease",
                    "arguments": {
                        "operation_id": "11111111-1111-4111-8111-111111111111"
                    },
                },
            },
        }
    )
    fresh_run = action_agent_guidance(
        {
            "ok": True,
            "command": "read",
            "code": "OK",
            "allowed_actions": ["safe-reclaim"],
            "data": {
                "legal_next_step": "A fresh run may safe-reclaim the expired prior attempt.",
                "agent_action": {
                    "command": "safe-reclaim",
                    "arguments": {
                        "submission_id": "11111111-1111-4111-8111-111111111111",
                        "lease_id": "22222222-2222-4222-8222-222222222222",
                    },
                },
            },
        }
    )

    same_text = " ".join(same_run["instructions"] )
    fresh_text = " ".join(fresh_run["instructions"] )
    assert "same durable run" in same_text
    assert "Call renew-lease" in same_text
    assert "Call safe-reclaim" not in same_text
    assert "fresh run" in fresh_text
    assert "Call safe-reclaim" in fresh_text
    assert "Call renew-lease" not in fresh_text


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


def test_action_guidance_treats_verification_uncertainty_as_defensible_estimation_not_perfection():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "start",
        "code": "OK",
        "allowed_actions": ["inspect"],
        "data": {},
    })
    text = " ".join(guidance["instructions"])
    assert "reasonable defensible estimate" in text
    assert "do not invent false precision" in text
    assert "Uncertainty is blocking only" in text
    assert "one defensible estimate versus the limit" in text
    assert "plausible range" not in text


def test_action_guidance_keeps_human_review_compact_and_hides_protocol_mechanics():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "allowed_actions": [],
        "data": {
            "human_action": {
                "summary": "Review decision",
                "shell_command": "dish-admin review-inspect cycle-id",
            }
        },
    })
    text = " ".join(guidance["instructions"])
    assert "decision first" in text
    assert "Do not dump raw details" in text
    assert "do not print it unless Marco asks" in text


def test_action_guidance_compresses_recovery_only_handoff():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "allowed_actions": [],
        "data": {
            "human_action": {
                "kind": "recover-expired-lease",
                "summary": "Release stale lease",
                "shell_command": "dish-admin recover-lease operation-id",
            }
        },
    })
    text = " ".join(guidance["instructions"])
    assert "one short blocker/status sentence" in text
    assert "Do not explain leases" in text
    assert "do not print it unless Marco asks" in text


def test_action_guidance_turns_evidence_hold_into_plain_english_question():
    guidance = action_agent_guidance({
        "ok": True,
        "command": "inspect",
        "code": "OK",
        "allowed_actions": [],
        "data": {
            "human_action": {
                "kind": "supply-evidence",
                "summary": "Record evidence",
                "shell_command": "dish-admin supply-evidence operation-id --detail '<answer>'",
            }
        },
    })
    text = " ".join(guidance["instructions"])
    assert "actual missing fact in plain English" in text
    assert "route/scope/date/reason" in text
    assert "do not print it unless Marco asks" in text


def test_action_guidance_explains_fresh_request_after_governed_intent_confirmation():
    guidance = action_agent_guidance({
        "ok": False,
        "command": "reject",
        "code": "CONFIRMATION_REQUIRED",
        "allowed_actions": ["reject"],
        "data": {},
        "errors": [{"rule": "governed_change_intent_confirmation_required"}],
    })
    text = " ".join(guidance["instructions"])
    assert "No workflow or external effect was committed" in text
    assert "fresh request ID" in text


def test_action_guidance_routes_exact_human_review_repairs_to_semantic_proposals():
    guidance = action_agent_guidance({
        "ok": False,
        "command": "reject",
        "code": "CONFIRMATION_REQUIRED",
        "allowed_actions": ["reject"],
        "data": {},
        "errors": [{"rule": "human_review_preflight_required"}],
    })
    text = " ".join(guidance["instructions"])
    assert "reasonable defensible estimate" in text
    assert "Large correction" in text
    assert "Marco-only choice remains" in text
    assert "[nutrition-protein]" in text
    assert "rather than prose" in text


def test_action_guidance_spells_out_verification_correction_and_route_vocabularies() -> None:
    result = {
        "command": "inspect",
        "code": "OK",
        "allowed_actions": ["approve", "reject"],
        "data": {},
        "errors": [],
    }
    guidance = action_agent_guidance(result)
    text = " ".join(guidance["instructions"])
    assert "correction=none" in text
    assert "correction=small" in text
    assert "large, evidence, or human-review" in text
    assert "Small correction is not a rejection route" in text
