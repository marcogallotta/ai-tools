from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest

from dish_pg import models
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.repositories import DishRepository, ScalarMutationSource
from dish_tool.content_versions import content_identity
from dish_pg.shadow_evidence import (
    EVIDENCE_SCHEMA_VERSION,
    ShadowEvaluation,
    canonical_response,
    canonical_transition,
    compare_evidence,
)
from dish_pg.shadow_worker import _target_authority_state
from tests.support.postgresql.command import _port
from tests.support.postgresql.workflow import NOW, workflow_db


def _source_snapshot(*, phase: str | None) -> dict:
    rows = [] if phase is None else [{
        "operation_kind": "initial",
        "status": "open",
        "phase": phase,
        "terminal_outcome": None,
    }]
    return {
        "selected_tables": ["operations"],
        "tables": {"operations": rows},
    }


def _target_state(*, phase: str | None) -> dict:
    rows = [] if phase is None else [{
        "kind": "initial",
        "lifecycle": "open",
        "phase": phase,
        "terminal_outcome": None,
    }]
    return {"captured_domains": ["operations"], "domains": {"operations": rows}}


def _payload(*, post_phase: str, effects=None) -> dict:
    pre = _target_state(phase=None)
    post = _target_state(phase=post_phase)
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "response": {
            "ok": True,
            "command": "start",
            "code": "OK",
            "http_status": 200,
            "retryable": False,
            "data": {
                "task_id": str(uuid.uuid4()),
                "operation_id": str(uuid.uuid4()),
                "phase": post_phase,
                "state": "open",
                "allowed_actions": ["prepare"],
                "request_id": str(uuid.uuid4()),
            },
        },
        "pre_state": pre,
        "post_state": post,
        "effects": canonical_transition(pre, post) if effects is None else effects,
    }


def test_versioned_comparator_reconciles_transport_and_generated_id_schemas():
    source = {
        "ok": True,
        "command": "start",
        "code": "OK",
        "task_gid": "120000000000001",
        "submission_id": str(uuid.uuid4()),
        "state": "open",
        "retryable": False,
        "allowed_actions": ["prepare"],
        "data": {"legacy_diagnostics": {"ignored": True}},
        "errors": [],
    }
    parity, differences = compare_evidence(
        source_outcome=source,
        source_pre_state=_source_snapshot(phase=None),
        source_post_state=_source_snapshot(phase="prepare_required"),
        target_payload=_payload(post_phase="prepare_required"),
    )
    assert parity == "semantic"
    assert differences == []


def test_versioned_comparator_detects_wrong_post_state_even_when_response_matches():
    source = {
        "ok": True,
        "command": "start",
        "code": "OK",
        "task_gid": "120000000000001",
        "submission_id": str(uuid.uuid4()),
        "state": "open",
        "retryable": False,
        "allowed_actions": ["prepare"],
        "data": {},
        "errors": [],
    }
    target = _payload(post_phase="wrong_phase")
    target["response"]["data"]["phase"] = "prepare_required"
    parity, differences = compare_evidence(
        source_outcome=source,
        source_pre_state=_source_snapshot(phase=None),
        source_post_state=_source_snapshot(phase="prepare_required"),
        target_payload=target,
    )
    assert parity == "mismatch"
    assert {item["axis"] for item in differences} == {"post_state", "effects"}


def test_versioned_comparator_detects_wrong_effect_transition():
    source = {
        "ok": True,
        "command": "start",
        "code": "OK",
        "task_gid": "120000000000001",
        "submission_id": str(uuid.uuid4()),
        "state": "open",
        "retryable": False,
        "allowed_actions": ["prepare"],
        "data": {},
        "errors": [],
    }
    parity, differences = compare_evidence(
        source_outcome=source,
        source_pre_state=_source_snapshot(phase=None),
        source_post_state=_source_snapshot(phase="prepare_required"),
        target_payload=_payload(post_phase="prepare_required", effects={"changes": {}}),
    )
    assert parity == "mismatch"
    assert [item["axis"] for item in differences] == ["effects"]


def test_versioned_comparator_detects_wrong_pre_state_even_when_post_state_matches():
    source = {
        "ok": True,
        "command": "start",
        "code": "OK",
        "task_gid": "120000000000001",
        "submission_id": str(uuid.uuid4()),
        "state": "open",
        "retryable": False,
        "allowed_actions": ["prepare"],
        "data": {},
        "errors": [],
    }
    target = _payload(post_phase="prepare_required")
    target["pre_state"] = _target_state(phase="already_started")
    target["effects"] = canonical_transition(
        target["pre_state"], target["post_state"]
    )
    parity, differences = compare_evidence(
        source_outcome=source,
        source_pre_state=_source_snapshot(phase=None),
        source_post_state=_source_snapshot(phase="prepare_required"),
        target_payload=target,
    )
    assert parity == "mismatch"
    assert {item["axis"] for item in differences} == {"pre_state", "effects"}


def test_versioned_comparator_uses_shared_failure_contract_not_legacy_only_errors():
    source = {
        "ok": False,
        "command": "prepare",
        "code": "INVALID_STATE",
        "task_gid": None,
        "submission_id": None,
        "state": None,
        "retryable": False,
        "allowed_actions": [],
        "data": {"message": "legacy-specific wording"},
        "errors": [{"rule": "operation_not_found"}],
    }
    state = _target_state(phase="prepare_required")
    target = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "response": {
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
            "data": {"message": "postgres-specific wording", "trace": "ignored"},
        },
        "pre_state": state,
        "post_state": state,
        "effects": {"changes": {}},
    }
    snapshot = _source_snapshot(phase="prepare_required")
    parity, differences = compare_evidence(
        source_outcome=source,
        source_pre_state=snapshot,
        source_post_state=snapshot,
        target_payload=target,
    )
    assert parity == "semantic"
    assert differences == []


def test_versioned_comparator_refuses_omitted_target_domains_even_when_source_rows_are_empty():
    source = {
        "ok": False,
        "command": "prepare",
        "code": "INVALID_STATE",
        "retryable": False,
        "allowed_actions": [],
    }
    empty_source = _source_snapshot(phase=None)
    target = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "response": dict(source),
        "pre_state": {"captured_domains": [], "domains": {}},
        "post_state": {"captured_domains": [], "domains": {}},
        "effects": {"changes": {}},
    }
    parity, differences = compare_evidence(
        source_outcome=source,
        source_pre_state=empty_source,
        source_post_state=empty_source,
        target_payload=target,
    )
    assert parity == "gap"
    assert {item["axis"] for item in differences} == {"pre_state", "post_state"}


def test_versioned_comparator_preserves_duplicate_authority_rows():
    source_pre = _source_snapshot(phase=None)
    source_post = _source_snapshot(phase="prepare_required")
    source_post["tables"]["operations"].append(
        dict(source_post["tables"]["operations"][0])
    )
    target = _payload(post_phase="prepare_required")
    parity, differences = compare_evidence(
        source_outcome={
            "ok": True,
            "command": "start",
            "code": "OK",
            "retryable": False,
            "allowed_actions": ["prepare"],
        },
        source_pre_state=source_pre,
        source_post_state=source_post,
        target_payload=target,
    )
    assert parity == "mismatch"
    assert "post_state" in {item["axis"] for item in differences}


def _content_source_snapshot(*, identity: str, title: str, body: str) -> dict:
    return {
        "selected_tables": ["task_content_state"],
        "tables": {
            "task_content_state": [{
                "last_confirmed_identity": identity,
                "last_confirmed_title": title,
                "last_confirmed_notes": body,
            }]
        },
    }


def _content_target_state(*, identity: str, title: str, body: str) -> dict:
    return {
        "captured_domains": ["task_content"],
        "domains": {
            "task_content": [{
                "identity": identity,
                "title": title,
                "body": body,
            }]
        },
    }


def _content_target_payload(*, identity: str, title: str, body: str) -> dict:
    state = _content_target_state(identity=identity, title=title, body=body)
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "response": {
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
        },
        "pre_state": state,
        "post_state": state,
        "effects": {"changes": {}},
    }


def test_old_postgresql_nul_hash_compares_as_canonical_without_mutating_raw_evidence() -> None:
    title = "Warm potato salad"
    body = "Purpose: preserve the dark-launch identity regression shape.\nServe warm.\n"
    canonical = content_identity(title, body)
    legacy = hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()
    source = _content_source_snapshot(identity=canonical, title=title, body=body)
    target = _content_target_payload(identity=legacy, title=title, body=body)

    parity, differences = compare_evidence(
        source_outcome={
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
            "allowed_actions": [],
        },
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )

    assert parity == "semantic"
    assert differences == []
    assert target["pre_state"]["domains"]["task_content"][0]["identity"] == legacy
    assert target["post_state"]["domains"]["task_content"][0]["identity"] == legacy


def test_active_old_postgresql_content_version_projects_raw_then_compares_canonical(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    title = "Warm potato salad"
    body = "Purpose: preserve the dark-launch identity regression shape.\nServe warm.\n"
    canonical = content_identity(title, body)
    legacy = hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()
    source = _content_source_snapshot(identity=canonical, title=title, body=body)

    with session_scope(factory) as session:
        state = session.get(models.DishState, (context["generation_id"], task_id))
        membership = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        assert state is not None and membership is not None
        version = session.get(models.ContentVersion, state.current_content_version_id)
        assert version is not None
        legacy_version_id = next(ids)
        mutation = DishRepository(session, uuid_factory=lambda: next(ids)).begin_scalar_mutation(
            generation_id=context["generation_id"],
            task_id=task_id,
            expected_dish_version=state.dish_version,
            expected_placement_version=state.placement_version,
            expected_catalog_version_id=state.catalog_version_id,
            source=ScalarMutationSource(
                route="import",
                import_run_id=context["import_run_id"],
                occurred_at=NOW,
            ),
        )
        mutation.replace_content(
            title=title,
            body=body,
            identity_scheme=version.identity_scheme,
            content_identity=legacy,
            contract_binding_id=context["binding_id"],
            predecessor_content_version_id=version.content_version_id,
            content_version_id=legacy_version_id,
        )
        mutation.finalize()

        target_state = _target_authority_state(
            session,
            port=_port(session, ids),
            envelope=type(
                "Envelope",
                (),
                {
                    "source_post_state": source,
                    "source_pre_state": source,
                    "canonical_input": {"arguments": {"task_gid": "123456789"}},
                },
            )(),
            arguments={"task_gid": "123456789"},
            request_id=uuid.uuid4(),
            result=None,
        )

    assert target_state["domains"]["task_content"][0]["identity"] == legacy
    target = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "response": {
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
        },
        "pre_state": target_state,
        "post_state": target_state,
        "effects": {"changes": {}},
    }
    parity, differences = compare_evidence(
        source_outcome={
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
            "allowed_actions": [],
        },
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )
    assert parity == "semantic"
    assert differences == []
    assert target_state["domains"]["task_content"][0]["identity"] == legacy


def test_unknown_target_content_identity_remains_a_mismatch() -> None:
    title = "Warm potato salad"
    body = "Canonical body\n"
    canonical = content_identity(title, body)
    source = _content_source_snapshot(identity=canonical, title=title, body=body)
    target = _content_target_payload(identity="f" * 64, title=title, body=body)

    parity, differences = compare_evidence(
        source_outcome={
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
            "allowed_actions": [],
        },
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )

    assert parity == "mismatch"
    assert {item["axis"] for item in differences} == {"pre_state", "post_state"}
    assert target["post_state"]["domains"]["task_content"][0]["identity"] == "f" * 64


def test_exact_old_hash_does_not_hide_genuine_changed_content() -> None:
    source_title = "Warm potato salad"
    source_body = "Canonical body\n"
    target_body = "Canonical body\nChanged.\n"
    source_identity = content_identity(source_title, source_body)
    target_legacy = hashlib.sha256(
        f"{source_title}\0{target_body}".encode("utf-8")
    ).hexdigest()
    source = _content_source_snapshot(
        identity=source_identity, title=source_title, body=source_body
    )
    target = _content_target_payload(
        identity=target_legacy, title=source_title, body=target_body
    )

    parity, differences = compare_evidence(
        source_outcome={
            "ok": False,
            "command": "prepare",
            "code": "INVALID_STATE",
            "retryable": False,
            "allowed_actions": [],
        },
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )

    assert parity == "mismatch"
    assert {item["axis"] for item in differences} == {"pre_state", "post_state"}


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
    assert canonical_response(payload["response"])["facts"] == {
        "required_start_kind": "planning"
    }


def test_create_shadow_response_adds_legacy_planning_shape_without_protocol_redesign() -> None:
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
            source_outcome={
                "ok": True,
                "command": "create",
                "data": {"task_gid": source_task_gid},
            },
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
