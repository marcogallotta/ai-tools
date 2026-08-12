from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.shadow_evidence import ShadowEvaluation, canonical_response
from tests.support.postgresql.workflow import NOW


@pytest.mark.parametrize(
    ("code", "data"),
    [
        ("PLANNING_CONFIRMATION_NOT_YET_ISSUED", {}),
        (
            "CONFIRMATION_REQUIRED",
            {
                "intent_challenge_id": str(uuid.uuid4()),
                "required_intent_basis": ["user_requested", "agent_override"],
            },
        ),
    ],
)
def test_planning_confirmation_shadow_response_matches_legacy_shape(code, data) -> None:
    payload = ShadowEvaluation(
        response={
            "ok": False,
            "command": "start",
            "code": code,
            "http_status": 400,
            "data": data,
            "retryable": False,
        },
        pre_state={},
        post_state={},
        effects={},
    ).as_payload()

    assert payload["code"] == "CONFIRMATION_REQUIRED"
    assert payload["retryable"] is True
    assert payload["data"]["allowed_actions"] == ["start"]
    assert payload["data"]["required_start_kind"] == "planning"
    assert payload["response"] == {
        key: payload[key]
        for key in ("ok", "command", "code", "http_status", "data", "retryable")
    }
    assert canonical_response(payload["response"])["facts"] == {
        "required_start_kind": "planning"
    }


def test_create_shadow_response_keeps_ok_contract_and_adds_planning_facts() -> None:
    payload = ShadowEvaluation(
        response={
            "ok": True,
            "command": "create",
            "code": "OK",
            "http_status": 200,
            "data": {"task_id": str(uuid.uuid4()), "allowed_actions": []},
            "retryable": False,
        },
        pre_state={},
        post_state={},
        effects={},
    ).as_payload()

    assert payload["code"] == "OK"
    assert payload["retryable"] is False
    assert payload["data"]["allowed_actions"] == ["start"]
    assert payload["data"]["required_start_kind"] == "planning"
    canonical = canonical_response(payload["response"])
    assert canonical["allowed_actions"] == ["start"]
    assert canonical["facts"] == {"required_start_kind": "planning"}


def _add_shadow_envelope(
    session,
    *,
    envelope_id: uuid.UUID,
    baseline_id: uuid.UUID,
    command_name: str,
    request_identity: str,
    canonical_input: dict,
    source_outcome: dict,
    source_pre_state: dict,
    captured_at,
    capture_qualification: str,
) -> None:
    session.add(
        tx.ShadowEnvelope(
            envelope_id=envelope_id,
            shadow_baseline_id=baseline_id,
            command_name=command_name,
            source_request_identity=request_identity,
            canonical_input=canonical_input,
            canonical_input_sha256="a" * 64,
            source_outcome=source_outcome,
            source_outcome_sha256="b" * 64,
            source_post_state={},
            rollout_sequence=None,
            source_authority_generation="legacy-generation",
            source_execution_identity=request_identity,
            principal=None,
            source_pre_state=source_pre_state,
            source_pre_state_sha256=None,
            pinned_inputs=None,
            source_effects=None,
            capture_qualification=capture_qualification,
            source_post_state_sha256=None,
            envelope_schema_version=1,
            captured_at=captured_at,
        )
    )


def test_capture_only_create_cascade_gets_distinct_gap_classification(workflow_db) -> None:
    factory, _ids, context, _task_id = workflow_db
    baseline_id = uuid.uuid4()
    create_envelope_id = uuid.uuid4()
    failing_envelope_id = uuid.uuid4()
    source_operation_id = str(uuid.uuid4())
    source_task_gid = "capture-only-created-task"

    with session_scope(factory) as session:
        session.add(
            tx.ShadowBaseline(
                shadow_baseline_id=baseline_id,
                generation_id=context["generation_id"],
                source_generation_identity="legacy-generation",
                source_commit="c" * 64,
                baseline_sequence=1,
                status="open",
                disqualification_reason=None,
                created_at=NOW,
                terminal_at=None,
            )
        )
        _add_shadow_envelope(
            session,
            envelope_id=create_envelope_id,
            baseline_id=baseline_id,
            command_name="create",
            request_identity="create-request",
            canonical_input={"arguments": {"title": "capture-only task"}},
            source_outcome={"ok": True, "command": "create", "data": {"task_gid": source_task_gid}},
            source_pre_state={},
            captured_at=NOW,
            capture_qualification="capture_only",
        )
        _add_shadow_envelope(
            session,
            envelope_id=failing_envelope_id,
            baseline_id=baseline_id,
            command_name="submit",
            request_identity="submit-request",
            canonical_input={"arguments": {"operation_id": source_operation_id}},
            source_outcome={"ok": True, "command": "submit", "data": {}},
            source_pre_state={
                "tables": {
                    "operations": [
                        {
                            "operation_id": source_operation_id,
                            "task_gid": source_task_gid,
                        }
                    ]
                }
            },
            captured_at=NOW + timedelta(seconds=1),
            capture_qualification="execute",
        )
        session.flush()

        cascade_gap = tx.ShadowGap(
            gap_id=uuid.uuid4(),
            shadow_baseline_id=baseline_id,
            envelope_id=failing_envelope_id,
            gap_identity="delivery:submit-request:revision:2",
            gap_kind="delivery_failure",
            state="open",
            details={
                "error": "no unique target operation binding for captured field operation_id",
                "failed_delivery_revision": 2,
            },
            resolution=None,
            gap_revision=1,
            created_at=NOW + timedelta(seconds=2),
            resolved_at=None,
        )
        session.add(cascade_gap)
        session.flush()

        assert cascade_gap.gap_kind == "uncomparable"
        assert cascade_gap.details["error_classification"] == (
            "unbound_create_operation_binding_cascade"
        )
        assert cascade_gap.details["source_task_gid"] == source_task_gid
        assert cascade_gap.details["create_source_request_identity"] == "create-request"
        assert cascade_gap.details["create_capture_qualification"] == "capture_only"
        assert cascade_gap.details["create_delivery_state"] is None

        unrelated_gap = tx.ShadowGap(
            gap_id=uuid.uuid4(),
            shadow_baseline_id=baseline_id,
            envelope_id=failing_envelope_id,
            gap_identity="delivery:submit-request:revision:3",
            gap_kind="delivery_failure",
            state="open",
            details={"error": "unrelated shadow evaluation failure"},
            resolution=None,
            gap_revision=1,
            created_at=NOW + timedelta(seconds=3),
            resolved_at=None,
        )
        session.add(unrelated_gap)
        session.flush()

        assert unrelated_gap.gap_kind == "delivery_failure"
        assert "error_classification" not in unrelated_gap.details
