"""Versioned, cross-backend dark-launch evidence comparison.

The source and target response/storage schemas intentionally differ.  This
module compares a bounded semantic projection instead of raw transport or row
shape, while preserving each raw target result in ``shadow_comparisons``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from dish_tool.content_versions import content_identity

EVIDENCE_SCHEMA_VERSION = 2
_REQUEST_ID_KEYS = {
    "request_id",
    "originating_request_id",
    "source_request_id",
    "claiming_request_id",
    "issuing_request_id",
    "consumed_by_request_id",
    "reservation_request_id",
}
_OPERATION_ID_KEYS = {
    "operation_id",
    "submission_id",
    "target_operation_id",
    "prepared_operation_id",
    "successor_operation_id",
    "source_operation_id",
    "blocking_operation_id",
    "open_operation_id",
}
_CYCLE_ID_KEYS = {
    "cycle_id",
    "target_cycle_id",
    "prepared_cycle_id",
    "source_cycle_id",
    "new_cycle_id",
    "reopened_from",
}
_LEASE_ID_KEYS = {"lease_id", "source_lease_id"}
_CONTENT_VERSION_ID_KEYS = {
    "content_version_id",
    "signed_content_version_id",
    "corrected_content_version_id",
    "held_content_version_id",
    "resumed_content_version_id",
    "reviewed_content_version_id",
    "baseline_content_version_id",
}
_SECTION_ID_KEYS = {
    "section_id",
    "destination_section_id",
}
_COMPARABLE_RESPONSE_KEYS = {
    "ok",
    "command",
    "code",
    "http_status",
    "retryable",
    "state",
    "phase",
    "allowed_actions",
    "data",
    "errors",
}
_SOURCE_DATA_KEYS = {
    "task_id",
    "operation_id",
    "cycle_id",
    "lease_id",
    "content_version_id",
    "section_id",
    "destination_section_id",
    "signed_content_version_id",
    "corrected_content_version_id",
    "hold_id",
    "requirement_id",
    "decision_id",
    "signoff_id",
    "inspection_id",
    "projection_event_id",
    "placement_projection_event_id",
    "completion_state",
    "completion_reason",
    "completed",
    "phase",
    "state",
    "allowed_actions",
}
_SOURCE_TOP_LEVEL_ALIASES = {
    "task_gid": "task_id",
    "submission_id": "operation_id",
    "destination_section_gid": "destination_section_id",
}
_IGNORED_RESPONSE_DATA_KEYS = {
    "request_id",
    "dish_id",
    "task_gid",
    "registry_version_id",
    "registry_revision",
    "identity_binding",
    "projection_freshness",
}
_IGNORED_RESULT_DATA_KEYS = {
    "request_id",
    "dish_id",
    "task_gid",
    "registry_version_id",
    "registry_revision",
    "identity_binding",
    "projection_freshness",
}


class ShadowEvidenceError(RuntimeError):
    """Evidence could not be compared under the current schema contract."""


@dataclass(frozen=True)
class ShadowEvaluation:
    response: Mapping[str, Any]
    pre_state: Mapping[str, Any]
    post_state: Mapping[str, Any]
    effects: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "response": dict(self.response),
            "pre_state": _canonical_target_state(self.pre_state),
            "post_state": _canonical_target_state(self.post_state),
            "effects": _canonical_target_effects(self.effects),
        }


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_payload(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_payload(item) for item in value]
    return value


def _sorted_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_canonical_payload(dict(row)) for row in rows]
    return sorted(normalized, key=lambda row: repr(row))


def _canonical_target_content_value(value: Mapping[str, Any]) -> dict[str, Any]:
    """Decode the one historical PostgreSQL task-content identity representation.

    This is a read-only storage compatibility decoder, not a comparator ignore:
    title/body remain in the evidence and only an identity that exactly proves
    the former ``title + NUL + body`` serialization is translated through the
    single canonical source ``content_identity`` authority. Unknown/corrupt
    identities are left untouched so they remain visible as parity gaps.
    """

    clean = dict(value)
    title = clean.get("title")
    body = clean.get("body")
    stored_identity = clean.get("identity")
    if not all(isinstance(item, str) for item in (title, body, stored_identity)):
        return clean
    legacy_identity = hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()
    if stored_identity == legacy_identity:
        clean["identity"] = content_identity(title, body)
    return clean


def _canonical_target_state(state: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(state)
    domains = clean.get("domains")
    if not isinstance(domains, Mapping):
        return clean
    canonical_domains = dict(domains)
    rows = canonical_domains.get("task_content")
    if isinstance(rows, (list, tuple)):
        canonical_domains["task_content"] = [
            _canonical_target_content_value(row) if isinstance(row, Mapping) else row
            for row in rows
        ]
    clean["domains"] = canonical_domains
    return clean


def _canonical_target_effects(effects: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(effects)
    changes = clean.get("task_content")
    if not isinstance(changes, (list, tuple)):
        return clean
    canonical_changes: list[Any] = []
    for change in changes:
        if not isinstance(change, Mapping):
            canonical_changes.append(change)
            continue
        canonical_change = dict(change)
        for side in ("before", "after"):
            value = canonical_change.get(side)
            if isinstance(value, Mapping):
                canonical_change[side] = _canonical_target_content_value(value)
        canonical_changes.append(canonical_change)
    clean["task_content"] = canonical_changes
    return clean


def canonical_response(payload: Mapping[str, Any], *, source: bool) -> dict[str, Any]:
    """Project backend-specific command responses onto shared semantics."""

    if source:
        source_data = payload.get("data")
        data: dict[str, Any] = (
            dict(source_data) if isinstance(source_data, Mapping) else {}
        )
        for key, canonical_key in _SOURCE_TOP_LEVEL_ALIASES.items():
            value = payload.get(key)
            if value is not None:
                data.setdefault(canonical_key, value)
        for key in _SOURCE_DATA_KEYS:
            if key in payload and payload[key] is not None:
                data.setdefault(key, payload[key])
        raw: dict[str, Any] = {
            "ok": bool(payload.get("ok")),
            "command": payload.get("command"),
            "code": payload.get("code"),
            "http_status": int(payload.get("http_status", 200 if payload.get("ok") else 409)),
            "retryable": bool(payload.get("retryable", False)),
            "state": payload.get("state"),
            "phase": payload.get("phase"),
            "allowed_actions": payload.get("allowed_actions"),
            "data": data,
            "errors": payload.get("errors") or [],
        }
    else:
        raw = {
            key: payload[key]
            for key in _COMPARABLE_RESPONSE_KEYS
            if key in payload
        }
        raw.setdefault("errors", [])
        raw.setdefault("data", {})
        raw.setdefault("retryable", False)

    normalized = _normalize_response_value(raw)
    data = normalized.get("data")
    if isinstance(data, Mapping):
        clean_data = dict(data)
        for key in _IGNORED_RESPONSE_DATA_KEYS:
            clean_data.pop(key, None)
        normalized["data"] = clean_data
    if normalized.get("data") == {}:
        normalized.pop("data", None)
    if normalized.get("errors") == []:
        normalized.pop("errors", None)
    if normalized.get("allowed_actions") in (None, []):
        normalized.pop("allowed_actions", None)
    if normalized.get("state") is None:
        normalized.pop("state", None)
    if normalized.get("phase") is None:
        normalized.pop("phase", None)
    return normalized


def _normalize_response_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            name = str(raw_key)
            if name in _REQUEST_ID_KEYS:
                normalized[name] = "<request>" if item is not None else None
            elif name in _OPERATION_ID_KEYS:
                normalized[name] = "<operation>" if item is not None else None
            elif name in _CYCLE_ID_KEYS:
                normalized[name] = "<cycle>" if item is not None else None
            elif name in _LEASE_ID_KEYS:
                normalized[name] = "<lease>" if item is not None else None
            elif name in _CONTENT_VERSION_ID_KEYS:
                normalized[name] = "<content-version>" if item is not None else None
            elif name in _SECTION_ID_KEYS:
                normalized[name] = "<section>" if item is not None else None
            elif name.endswith("_id") and item is not None:
                normalized[name] = f"<{name[:-3] or 'id'}>"
            else:
                normalized[name] = _normalize_response_value(item, name)
        return normalized
    if isinstance(value, (list, tuple)):
        normalized_items = [_normalize_response_value(item, key) for item in value]
        if key in {"allowed_actions", "authorization_grant_ids", "copied_authorization_grant_ids"}:
            return sorted(normalized_items, key=repr)
        return normalized_items
    return value


def _canonical_task_content(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "identity": row.get("last_confirmed_identity"),
            "title": row.get("last_confirmed_title"),
            "body": row.get("last_confirmed_notes"),
        }
        for row in rows
    )


def _canonical_placements(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "task_ref": row.get("task_gid") or "<task>",
            "section_ref": row.get("last_confirmed_section_gid"),
        }
        for row in rows
    )


def _canonical_completion(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "completed": bool(row.get("completed")),
            "reason": row.get("completion_reason"),
        }
        for row in rows
    )


def _canonical_operations(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "kind": row.get("operation_kind") or row.get("kind"),
            "lifecycle": row.get("status") or row.get("lifecycle"),
            "phase": _normalize_phase(row.get("phase")),
            "terminal_outcome": row.get("terminal_outcome"),
        }
        for row in rows
    )


def _normalize_phase(value: Any) -> Any:
    if value == "initial":
        return "prepare_required"
    return value


def _canonical_cycles(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "sequence": row.get("cycle_sequence"),
            "lifecycle": row.get("lifecycle"),
            "outcome": row.get("outcome"),
            "reviewed_content_ref": (
                "<content-version>"
                if row.get("reviewed_content_version_id") is not None
                else None
            ),
        }
        for row in rows
    )


def _canonical_leases(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "lease_kind": row.get("lease_kind"),
            "actor_role": row.get("actor_role"),
            "state": row.get("state"),
            "owner_id": row.get("owner_id"),
        }
        for row in rows
    )


def _canonical_evidence_holds(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "state": row.get("state"),
            "reason": row.get("reason"),
            "resolution": _canonical_payload(row.get("resolution")),
        }
        for row in rows
    )


def _canonical_human_review(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "state": row.get("state"),
            "route": row.get("route"),
            "question": row.get("question"),
        }
        for row in rows
    )


def _canonical_signoffs(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "signoff_kind": row.get("signoff_kind"),
            "signed_content_ref": (
                "<content-version>"
                if row.get("signed_content_version_id") is not None
                else None
            ),
        }
        for row in rows
    )


def _canonical_abandonment(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(
        {
            "state": row.get("state"),
            "reason": row.get("reason"),
            "has_successor": row.get("successor_operation_id") is not None,
        }
        for row in rows
    )


_SOURCE_DOMAIN_PROJECTORS = {
    "task_content_state": ("task_content", _canonical_task_content),
    "current_task_section_placement": ("placements", _canonical_placements),
    "task_completion_state": ("completion", _canonical_completion),
    "operations": ("operations", _canonical_operations),
    "verification_cycles": ("verification_cycles", _canonical_cycles),
    "leases": ("leases", _canonical_leases),
    "evidence_holds": ("evidence_holds", _canonical_evidence_holds),
    "human_review_requirements": ("human_review", _canonical_human_review),
    "verification_signoffs": ("verification_signoffs", _canonical_signoffs),
    "abandonment_attempts": ("abandonment", _canonical_abandonment),
}


def canonical_legacy_state(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    selected = list(snapshot.get("selected_tables") or [])
    tables = snapshot.get("tables") or {}
    domains: dict[str, Any] = {}
    captured_domains: list[str] = []
    for table_name in selected:
        projector_entry = _SOURCE_DOMAIN_PROJECTORS.get(str(table_name))
        if projector_entry is None:
            continue
        domain_name, projector = projector_entry
        rows = tables.get(table_name) or []
        if not isinstance(rows, list):
            raise ShadowEvidenceError(f"source table {table_name!r} is not row evidence")
        domains[domain_name] = projector(rows)
        captured_domains.append(domain_name)
    return {
        "captured_domains": sorted(set(captured_domains)),
        "domains": domains,
    }


def canonical_transition(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    before_domains = before.get("domains") or {}
    after_domains = after.get("domains") or {}
    captured = sorted(
        set(before.get("captured_domains") or [])
        | set(after.get("captured_domains") or [])
    )
    effects: dict[str, Any] = {}
    for domain in captured:
        left = _canonical_payload(before_domains.get(domain, []))
        right = _canonical_payload(after_domains.get(domain, []))
        if left != right:
            effects[domain] = {"before": left, "after": right}
    return effects


def compare_evidence(
    *,
    source_outcome: Mapping[str, Any],
    source_pre_state: Mapping[str, Any],
    source_post_state: Mapping[str, Any],
    target_payload: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    schema_version = target_payload.get("evidence_schema_version")
    if schema_version != EVIDENCE_SCHEMA_VERSION:
        raise ShadowEvidenceError(
            f"unsupported target evidence schema version: {schema_version!r}"
        )
    source_response = canonical_response(source_outcome, source=True)
    target_response = canonical_response(target_payload.get("response") or {}, source=False)
    source_pre = canonical_legacy_state(source_pre_state)
    source_post = canonical_legacy_state(source_post_state)
    target_pre = target_payload.get("pre_state") or {}
    target_post = target_payload.get("post_state") or {}
    target_effects = target_payload.get("effects") or {}
    differences: list[dict[str, Any]] = []

    for name, left, right in (
        ("response", source_response, target_response),
        ("pre_state", source_pre, target_pre),
        ("post_state", source_post, target_post),
        ("effects", canonical_transition(source_pre, source_post), target_effects),
    ):
        canonical_left = _canonical_payload(left)
        canonical_right = _canonical_payload(right)
        if canonical_left != canonical_right:
            differences.append(
                {
                    "surface": name,
                    "source": canonical_left,
                    "target": canonical_right,
                }
            )
    return ("semantic", []) if not differences else ("mismatch", differences)


def comparable_result_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one stored command result payload for shadow metadata only."""

    clean = dict(data)
    for key in _IGNORED_RESULT_DATA_KEYS:
        clean.pop(key, None)
    return _normalize_response_value(clean)
