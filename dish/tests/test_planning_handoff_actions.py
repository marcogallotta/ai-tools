from __future__ import annotations

import copy
import sqlite3

from dish_service.action_guidance import attach_action_agent_guidance
from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from tests.support.planning_intent import confirmed_planning_start
from tests.support.planning import Backend, PLANNING, release


def test_postgresql_native_planning_handoff_gets_dish_id_continuation():
    dish_id = "11111111-1111-4111-8111-111111111111"
    prepared = {
        "ok": True,
        "command": "prepare",
        "code": "OK",
        "http_status": 200,
        "retryable": False,
        "data": {
            "dish_id": dish_id,
            "handoff": "planning-to-research",
            "required_start_kind": "initial",
            "allowed_actions": ["start"],
        },
    }

    guided = attach_action_agent_guidance(prepared)

    assert guided["data"]["agent_action"] == {
        "command": "start",
        "arguments": {"dish_id": dish_id, "kind": "initial"},
    }
    assert guided["data"]["continuation_requirements"] == {
        "fresh_client_run_id": True,
        "fresh_client_request_id": True,
        "omit_arguments": ["prepared_operation_id"],
    }


def test_service_preserves_planning_handoff_start_contract(tmp_path):
    backend = Backend(task_gid="123456789")
    honest = tmp_path / "honest"
    honest.mkdir()
    (honest / "dish-verification-protocol.md").write_text(
        "verification protocol", encoding="utf-8"
    )
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "dish.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=lambda role=None: release(honest, role),
    )
    planning_principal = ServicePrincipal(
        owner_id="action",
        run_id="11111111-1111-4111-8111-111111111111",
    )
    research_principal = ServicePrincipal(
        owner_id="action",
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    started = confirmed_planning_start(
        service,
        {"agent": "gpt", "task_gid": "123456789", "kind": "planning"},
        principal=planning_principal,
        challenge_request_id="22222222-2222-4222-8222-222222222222",
        start_request_id="77777777-7777-4777-8777-777777777777",
    )
    assert started["ok"], started

    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": PLANNING,
        },
        principal=planning_principal,
        request_id="33333333-3333-4333-8333-333333333333",
    )

    assert prepared["ok"], prepared
    assert prepared["data"]["handoff"] == "planning-to-research"
    assert prepared["allowed_actions"] == ["start"]
    assert prepared["data"]["required_start_kind"] == "initial"
    assert prepared["data"]["service_access"] == {"state": "handoff"}

    action_prepared = attach_action_agent_guidance(copy.deepcopy(prepared))
    assert action_prepared["data"]["agent_action"] == {
        "command": "start",
        "arguments": {"task_gid": "123456789", "kind": "initial"},
    }
    assert action_prepared["data"]["continuation_requirements"] == {
        "fresh_client_run_id": True,
        "fresh_client_request_id": True,
        "omit_arguments": ["prepared_operation_id"],
    }
    handoff_guidance = " ".join(
        action_prepared["data"]["agent_guidance"]["instructions"]
    )
    assert "normal Planning→Research handoff is non-prepared" in handoff_guidance
    assert "fresh client.run_id" in handoff_guidance
    assert "omit prepared_operation_id" in handoff_guidance

    with sqlite3.connect(service.config.db_path) as conn:
        conn.row_factory = sqlite3.Row
        planning = conn.execute(
            """SELECT status, phase, terminal_outcome, run_id
                 FROM operations WHERE operation_id=?""",
            (started["submission_id"],),
        ).fetchone()
        assert planning is not None
        assert dict(planning) == {
            "status": "completed",
            "phase": "terminal",
            "terminal_outcome": "planning_handoff_confirmed",
            "run_id": planning_principal.run_id,
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM abandonment_attempts WHERE source_operation_id=?",
            (started["submission_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_successions WHERE source_operation_id=?",
            (started["submission_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM safe_reclaims WHERE source_operation_id=?",
            (started["submission_id"],),
        ).fetchone()[0] == 0

    repeated_planning = confirmed_planning_start(
        service,
        {"agent": "gpt", "task_gid": "123456789", "kind": "planning"},
        principal=planning_principal,
        challenge_request_id="44444444-4444-4444-8444-444444444444",
        start_request_id="88888888-8888-4888-8888-888888888888",
    )

    assert repeated_planning["code"] == "VALIDATION_FAILED"
    assert repeated_planning["retryable"] is True
    assert repeated_planning["allowed_actions"] == ["start"]
    assert repeated_planning["errors"] == [
        {
            "rule": "planning_handoff_requires_initial",
            "required_start_kind": "initial",
            "legal_next_step": (
                "start with kind=initial using a fresh client.request_id; "
                "do not start Planning again"
            ),
        }
    ]
    assert repeated_planning["data"]["required_start_kind"] == "initial"
    assert repeated_planning["data"]["legal_next_step"] == (
        "start with kind=initial using a fresh client.request_id; "
        "do not start Planning again"
    )

    mistaken_prepared = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "123456789",
            "kind": "initial",
            "prepared_operation_id": started["submission_id"],
        },
        principal=research_principal,
        request_id="99999999-9999-4999-8999-999999999999",
    )
    assert mistaken_prepared["code"] == "VALIDATION_FAILED"
    assert mistaken_prepared["retryable"] is True
    assert mistaken_prepared["allowed_actions"] == ["start"]
    mistaken_error = mistaken_prepared["errors"][0]
    assert mistaken_error["rule"] == "planning_handoff_requires_initial"
    assert mistaken_error["prepared_operation_id"] == started["submission_id"]
    assert mistaken_error["fresh_run_required"] is True
    assert mistaken_error["prepared_operation_id_allowed"] is False
    assert mistaken_prepared["data"]["required_start_kind"] == "initial"
    assert "omitting prepared_operation_id" in mistaken_prepared["data"]["legal_next_step"]
    assert "fresh client.run_id" in mistaken_prepared["data"]["legal_next_step"]
    assert "prepared_successor_not_found" not in str(mistaken_prepared)

    mistaken_action = attach_action_agent_guidance(copy.deepcopy(mistaken_prepared))
    mistaken_guidance = " ".join(
        mistaken_action["data"]["agent_guidance"]["instructions"]
    )
    assert "Planning is complete" in mistaken_guidance
    assert "fresh client.run_id" in mistaken_guidance
    assert "Omit prepared_operation_id" in mistaken_guidance

    research = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "123456789", "kind": "initial"},
        principal=research_principal,
        request_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    )
    assert research["ok"], research
    assert research["submission_id"] != started["submission_id"]
    assert research["data"]["operation_kind"] == "initial"

    with sqlite3.connect(service.config.db_path) as conn:
        conn.row_factory = sqlite3.Row
        research_op = conn.execute(
            "SELECT status, operation_kind, run_id FROM operations WHERE operation_id=?",
            (research["submission_id"],),
        ).fetchone()
        assert research_op is not None
        assert dict(research_op) == {
            "status": "open",
            "operation_kind": "initial",
            "run_id": research_principal.run_id,
        }
        assert research_op["run_id"] != planning_principal.run_id
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_successions WHERE source_operation_id=?",
            (started["submission_id"],),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM safe_reclaims WHERE source_operation_id=?",
            (started["submission_id"],),
        ).fetchone()[0] == 0
