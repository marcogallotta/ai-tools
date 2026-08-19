from __future__ import annotations

import hashlib

from dish_pg.shadow_evidence import EVIDENCE_SCHEMA_VERSION, compare_evidence
from dish_tool.content_versions import content_identity


def _source(identity: str, title: str, body: str) -> dict:
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


def _target(identity: str, title: str, body: str) -> dict:
    state = {
        "captured_domains": ["task_content"],
        "domains": {"task_content": [{"identity": identity, "title": title, "body": body}]},
    }
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "response": {"ok": False, "command": "prepare", "code": "INVALID_STATE", "retryable": False},
        "pre_state": state,
        "post_state": state,
        "effects": {"changes": {}},
    }


def _source_outcome() -> dict:
    return {
        "ok": False,
        "command": "prepare",
        "code": "INVALID_STATE",
        "retryable": False,
        "allowed_actions": [],
    }


def test_exact_old_nul_hash_compares_as_canonical_without_mutating_raw_evidence() -> None:
    title = "Warm potato salad"
    body = "Purpose: preserve the dark-launch identity regression shape.\nServe warm.\n"
    canonical = content_identity(title, body)
    old = hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()
    source = _source(canonical, title, body)
    target = _target(old, title, body)

    parity, differences = compare_evidence(
        source_outcome=_source_outcome(),
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )

    assert parity == "semantic"
    assert differences == []
    assert target["post_state"]["domains"]["task_content"][0]["identity"] == old


def test_unknown_identity_remains_a_mismatch() -> None:
    title = "Warm potato salad"
    body = "Canonical body\n"
    source = _source(content_identity(title, body), title, body)
    target = _target("f" * 64, title, body)

    parity, differences = compare_evidence(
        source_outcome=_source_outcome(),
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )

    assert parity == "mismatch"
    assert {item["axis"] for item in differences} == {"pre_state", "post_state"}


def test_exact_old_hash_does_not_hide_changed_content() -> None:
    title = "Warm potato salad"
    source_body = "Canonical body\n"
    target_body = "Canonical body\nChanged.\n"
    source = _source(content_identity(title, source_body), title, source_body)
    target_old = hashlib.sha256(f"{title}\0{target_body}".encode("utf-8")).hexdigest()
    target = _target(target_old, title, target_body)

    parity, differences = compare_evidence(
        source_outcome=_source_outcome(),
        source_pre_state=source,
        source_post_state=source,
        target_payload=target,
    )

    assert parity == "mismatch"
    assert {item["axis"] for item in differences} == {"pre_state", "post_state"}
