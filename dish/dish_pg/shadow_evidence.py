"""Versioned, cross-backend dark-launch evidence comparison.

The source and target response/storage schemas intentionally differ.  This
module compares a bounded semantic projection instead of raw transport or row
shape, while preserving each raw target result in ``shadow_comparisons``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .dark_launch_parity import legacy_compatible_shadow_response

EVIDENCE_SCHEMA_VERSION = 2

_IDENTIFIER_FAMILIES = {
    "task_id": "task",
    "operation_id": "operation",
    "successor_operation_id": "operation",
    "lease_id": "lease",
    "cycle_id": "verification_cycle",
    "challenge_id": "planning_challenge",
    "abandonment_id": "abandonment",
    "attempt_id": "abandonment",
    "requirement_id": "human_review_requirement",
    "hold_id": "evidence_hold",
    "grant_id": "authorization_grant",
    "content_version_id": "content_version",
}
_RESPONSE_FACT_KEYS = {
    "required_action",
    "required_start_kind",
    "decision",
    "outcome",
}

_DOMAIN_BY_TABLE = {
    "task_content_state": "task_content",
    "operations": "operations",
    "service_leases": "leases",
    "verification_cycles": "verification_cycles",
    "write_attempts": "external_intents",
    "movement_attempts": "external_intents",
    "abandonment_attempts": "abandonments",
    "service_requests": "requests",
}


@dataclass(frozen=True)
class ShadowEvaluation:
    response: Mapping[str, Any]
    pre_state: Mapping[str, Any]
    post_state: Mapping[str, Any]
    effects: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        response = legacy_compatible_shadow_response(self.response)
        payload = dict(response)
        payload.update({
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "response": response,
            "pre_state": dict(self.pre_state),
            "post_state": dict(self.post_state),
            "effects": dict(self.effects),
        })
        return payload


def _stable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _stable(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_stable(child) for child in value]
    return value


def _sorted_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Sort deterministically while preserving semantic row multiplicity."""
    clean = [_stable(dict(row)) for row in rows]
    return sorted(
        clean,
        key=lambda row: json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ),
    )


def _normalize_phase(value: Any, lifecycle: Any = None) -> Any:
    text = None if value is None else str(value)
    if text in {"completed", "cancelled"} or str(lifecycle or "") in {"completed", "cancelled"}:
        return "terminal"
    return text


def _normalize_lifecycle(value: Any) -> Any:
    text = None if value is None else str(value)
    if text in {"cancelled", "cancelled_by_marco", "abandoned"}:
        return "cancelled"
    if text == "failed":
        return "uncertain"
    return text


def _identity_marker(value: Any, family: str) -> Any:
    if value in {None, ""}:
        return None
    return {"$identity_family": family}


def _response_value(value: Mapping[str, Any], data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if value.get(key) is not None:
            return value[key]
        if data.get(key) is not None:
            return data[key]
    return None


def canonical_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Map both transports into the deliberately small shared result contract.

    Backend-specific diagnostics and generated identifiers are retained in raw
    evidence but are not compared as if the two APIs had identical schemas.
    Generated identities compare by semantic family; lifecycle and legal
    actions must be supplied by each authority rather than inferred from phase.
    """
    data = dict(value.get("data") or {}) if isinstance(value.get("data"), Mapping) else {}
    task = _response_value(value, data, "task_id", "task_gid")
    operation = _response_value(value, data, "operation_id", "submission_id")
    state = _response_value(value, data, "state", "lifecycle")
    allowed = _response_value(value, data, "allowed_actions")
    if not isinstance(allowed, (list, tuple)):
        allowed = []
    facts = {
        key: _stable(data[key])
        for key in sorted(_RESPONSE_FACT_KEYS)
        if data.get(key) is not None
    }
    response: dict[str, Any] = {
        "ok": bool(value.get("ok")),
        "command": value.get("command"),
        "code": value.get("code"),
        "retryable": bool(value.get("retryable", False)),
    }
    # Optional axes are compared only when the source transport actually
    # exposes them.  The target may carry richer diagnostics without turning
    # a valid semantic match into a mismatch.
    if task not in {None, ""}:
        response["task"] = _identity_marker(task, "task")
    if operation not in {None, ""}:
        response["operation"] = _identity_marker(operation, "operation")
    if state is not None:
        response["state"] = state
    if allowed or bool(value.get("ok")):
        response["allowed_actions"] = sorted(str(item) for item in allowed)
    if facts:
        response["facts"] = facts
    return response


def _legacy_selected_tables(snapshot: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(snapshot, Mapping):
        return set()
    explicit = snapshot.get("selected_tables")
    if isinstance(explicit, list):
        return {str(value) for value in explicit}
    tables = snapshot.get("tables")
    return set(tables) if isinstance(tables, Mapping) else set()


def canonical_legacy_state(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project the bounded SQLite snapshot into backend-neutral authority facts."""
    if not isinstance(snapshot, Mapping):
        return {"captured_domains": [], "domains": {}}
    tables = snapshot.get("tables")
    tables = dict(tables) if isinstance(tables, Mapping) else {}
    selected = _legacy_selected_tables(snapshot)
    domains: dict[str, Any] = {}

    if "task_content_state" in selected:
        domains["task_content"] = _sorted_rows([
            {
                "identity": row.get("last_confirmed_identity"),
                "title": row.get("last_confirmed_title"),
                "body": row.get("last_confirmed_notes"),
            }
            for row in tables.get("task_content_state", [])
        ])
    if "operations" in selected:
        domains["operations"] = _sorted_rows([
            {
                "kind": row.get("operation_kind"),
                "lifecycle": _normalize_lifecycle(row.get("status")),
                "phase": _normalize_phase(row.get("phase"), row.get("status")),
                "terminal_outcome": row.get("terminal_outcome"),
            }
            for row in tables.get("operations", [])
        ])
    if "service_leases" in selected:
        domains["leases"] = _sorted_rows([
            {
                "state": "terminal" if row.get("released_at") else "active",
                "lease_kind": row.get("lease_kind") or "actor",
                "actor_attempt_sequence": row.get("actor_attempt_seq"),
            }
            for row in tables.get("service_leases", [])
        ])
    if "verification_cycles" in selected:
        domains["verification_cycles"] = _sorted_rows([
            {
                "outcome": row.get("outcome"),
                "open": row.get("completed_at") is None,
            }
            for row in tables.get("verification_cycles", [])
        ])
    if {"write_attempts", "movement_attempts"} & selected:
        intents: list[dict[str, Any]] = []
        intents.extend({"kind": "content_write"} for _ in tables.get("write_attempts", []))
        intents.extend({"kind": "section_move"} for _ in tables.get("movement_attempts", []))
        domains["external_intents"] = _sorted_rows(intents)
    if "abandonment_attempts" in selected:
        legacy_abandonment_states = {
            "started": "preparing",
            "awaiting_hold_resolution": "blocked",
            "blocked_manual_reconciliation": "blocked",
            "awaiting_successor_claim": "published",
            "completed": "completed",
        }
        domains["abandonments"] = _sorted_rows([
            {"state": legacy_abandonment_states.get(row.get("status"), row.get("status"))}
            for row in tables.get("abandonment_attempts", [])
        ])
    if "service_requests" in selected:
        domains["requests"] = _sorted_rows([
            {"command": row.get("command"), "status": row.get("status")}
            for row in tables.get("service_requests", [])
        ])

    captured_domains = sorted({_DOMAIN_BY_TABLE[table] for table in selected if table in _DOMAIN_BY_TABLE})
    return {"captured_domains": captured_domains, "domains": domains}


def canonical_transition(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Return changed backend-neutral domains only."""
    before_domains = dict(before.get("domains") or {})
    after_domains = dict(after.get("domains") or {})
    changes: dict[str, Any] = {}
    for domain in sorted(set(before_domains) | set(after_domains)):
        if before_domains.get(domain) != after_domains.get(domain):
            changes[domain] = {
                "before": before_domains.get(domain, []),
                "after": after_domains.get(domain, []),
            }
    return {"changes": changes}


def _axis_difference(axis: str, source: Any, target: Any) -> dict[str, Any]:
    return {"axis": axis, "source": source, "target": target}


def compare_evidence(
    *,
    source_outcome: Mapping[str, Any],
    source_pre_state: Mapping[str, Any] | None,
    source_post_state: Mapping[str, Any],
    target_payload: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Compare response, pre/post authority state, and effects independently."""
    if target_payload.get("evidence_schema_version") != EVIDENCE_SCHEMA_VERSION:
        return "gap", [{"axis": "evidence", "reason": "unsupported target evidence schema"}]
    target_response = target_payload.get("response")
    target_pre = target_payload.get("pre_state")
    target_post = target_payload.get("post_state")
    target_effects = target_payload.get("effects")
    if not all(
        isinstance(value, Mapping)
        for value in (target_response, target_pre, target_post, target_effects)
    ):
        return "gap", [{"axis": "evidence", "reason": "target evidence is incomplete"}]

    differences: list[dict[str, Any]] = []
    source_response = canonical_response(source_outcome)
    canonical_target_response = canonical_response(target_response)
    target_response_projection = {
        key: canonical_target_response.get(key)
        for key in source_response
    }
    if source_response != target_response_projection:
        differences.append(_axis_difference("response", source_response, target_response_projection))

    source_post = canonical_legacy_state(source_post_state)
    source_pre = canonical_legacy_state(source_pre_state)
    captured_domains = list(source_post.get("captured_domains") or [])
    target_pre_captured = set(target_pre.get("captured_domains") or [])
    target_post_captured = set(target_post.get("captured_domains") or [])
    target_pre_domains = dict(target_pre.get("domains") or {})
    target_domains = dict(target_post.get("domains") or {})
    source_pre_domains = dict(source_pre.get("domains") or {})
    source_domains = dict(source_post.get("domains") or {})
    if not captured_domains:
        differences.append({"axis": "pre_state", "reason": "source snapshot has no comparable domains"})
        differences.append({"axis": "post_state", "reason": "source snapshot has no comparable domains"})
        state_comparable = False
    else:
        state_comparable = True
        required_domains = set(captured_domains)
        missing_target_pre = sorted(required_domains - target_pre_captured)
        missing_target_post = sorted(required_domains - target_post_captured)
        if missing_target_pre:
            differences.append({
                "axis": "pre_state",
                "reason": "target pre-state omitted comparable domains",
                "missing_domains": missing_target_pre,
            })
            state_comparable = False
        if missing_target_post:
            differences.append({
                "axis": "post_state",
                "reason": "target post-state omitted comparable domains",
                "missing_domains": missing_target_post,
            })
            state_comparable = False
        source_pre_projection = {
            domain: source_pre_domains.get(domain, [])
            for domain in captured_domains
        }
        target_pre_projection = {
            domain: target_pre_domains.get(domain, [])
            for domain in captured_domains
        }
        if source_pre_projection != target_pre_projection:
            differences.append(
                _axis_difference("pre_state", source_pre_projection, target_pre_projection)
            )
        source_projection = {domain: source_domains.get(domain, []) for domain in captured_domains}
        target_projection = {domain: target_domains.get(domain, []) for domain in captured_domains}
        if source_projection != target_projection:
            differences.append(_axis_difference("post_state", source_projection, target_projection))

    if not source_pre.get("captured_domains"):
        differences.append({"axis": "effects", "reason": "source pre-state has no comparable domains"})
        effects_comparable = False
    else:
        effects_comparable = True
        source_effects = canonical_transition(source_pre, source_post)
        if source_effects != target_effects:
            differences.append(_axis_difference("effects", source_effects, target_effects))

    hard_difference = any("source" in item and "target" in item for item in differences)
    if hard_difference:
        return "mismatch", differences
    if not state_comparable or not effects_comparable:
        return "gap", differences
    return "semantic", []
