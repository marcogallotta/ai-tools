from __future__ import annotations

import uuid

from dish_pg.shadow_evidence import (
    EVIDENCE_SCHEMA_VERSION,
    canonical_transition,
    compare_evidence,
)


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
