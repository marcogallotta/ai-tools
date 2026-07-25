"""Shared command backend protocol, trace state, and parsing helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .constants import (
    AGENT_FAMILIES,
    CHANGE_LEVELS,
    COOKING_PROJECT_GID,
    SUBMISSION_KINDS,
)
from .database import (
    create_submission,
    get_submission,
    latest_change_diff_telemetry,
    latest_successful_rejection_reason,
    record_audit,
    transition_submission,
)
from .errors import BackendFailure, DishRuleError
from .models import (
    ResolvedRelease,
    SectionRegistry,
    agent_family,
    is_protocol_managed,
    opposite_family,
    resolve_destination,
    utc_now,
)
from .recovery import begin_write_attempt, finish_write_attempt
from .results import allowed_actions_for_state, error_envelope, result_envelope
from .telemetry import calculate_change_diff
from .validation import (
    extract_exact_label_line,
    parse_canonical_title,
    validate_note,
    validate_title_declaration,
)


class CommandBackend(Protocol):
    def list_sections(self, project_gid: str) -> list[dict[str, Any]]: ...

    def read_task(self, task_gid: str) -> dict[str, Any]: ...

    def create_bare_task(
        self, *, title: str, project_gid: str, section_gid: str
    ) -> dict[str, Any]: ...

    def update_task_content(
        self, *, task_gid: str, title: str, notes: str
    ) -> None: ...

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None: ...


@dataclass
class CommandTrace:
    task_gid: str | None = None
    submission_id: str | None = None
    state: str | None = None
    known_submission: bool = False
    audit_details: dict[str, Any] = field(default_factory=dict)
    actor_agent: str | None = None


def _clean_required(value: Any, *, rule: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise DishRuleError(
            "INVALID_ARGUMENT", f"{label} is required", rule=rule
        )
    return clean


def _gid(value: Any) -> str | None:
    if isinstance(value, Mapping):
        clean = str(value.get("gid") or "").strip()
    else:
        clean = str(value or "").strip()
    return clean or None


def _task_is_in_project(task: Mapping[str, Any], project_gid: str) -> bool:
    projects = task.get("projects") or []
    if isinstance(projects, list) and any(_gid(project) == project_gid for project in projects):
        return True
    memberships = task.get("memberships") or []
    return isinstance(memberships, list) and any(
        isinstance(membership, Mapping)
        and _gid(membership.get("project")) == project_gid
        for membership in memberships
    )


def _task_section_gid(task: Mapping[str, Any], project_gid: str) -> str | None:
    memberships = task.get("memberships") or []
    if not isinstance(memberships, list):
        raise DishRuleError(
            "VALIDATION_FAILED",
            "task memberships are malformed",
            rule="task_membership_malformed",
        )
    matching = [
        membership
        for membership in memberships
        if isinstance(membership, Mapping)
        and _gid(membership.get("project")) == project_gid
    ]
    section_gids = {
        gid
        for membership in matching
        if (gid := _gid(membership.get("section"))) is not None
    }
    if len(section_gids) > 1:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "task has ambiguous Cooking section membership",
            rule="task_membership_ambiguous",
            details={"section_gids": sorted(section_gids)},
        )
    return next(iter(section_gids), None)


def _require_cooking_task(task: Mapping[str, Any], task_gid: str) -> None:
    if not _task_is_in_project(task, COOKING_PROJECT_GID):
        raise DishRuleError(
            "UNMANAGED_TASK",
            f"task {task_gid} is not in the Cooking project",
            rule="task_not_in_cooking",
        )


def _frozen_release_data(
    release: ResolvedRelease, submission_kind: str
) -> dict[str, Any]:
    """Transitional start payload without task-lifetime protocol authority."""

    role = "planning" if submission_kind == "planning" else "research"
    manifest_key = "planning" if submission_kind == "planning" else "complete_task"
    return {
        "protocol_version": release.protocol_version,
        "schema_version": release.schema_version,
        "stage_protocol": {role: release.protocol_for_role(role)},
        "task_schema": dict(release.schema),
        "legacy_validation_adapter": dict(
            release.manifest_for_submission(submission_kind)
        ),
        "legacy_validation_adapter_text": release.manifest_texts[manifest_key],
    }


def _decode_json(value: Any, *, field_name: str) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise DishRuleError(
            "INTERNAL_ERROR",
            f"stored submission field is malformed: {field_name}",
            rule="stored_submission_malformed",
            details={"field": field_name},
        ) from exc


def _title_arguments_present(
    *,
    dish_name: Any = None,
    recognition: Any = None,
    roles: Any = None,
    no_role_tags: bool = False,
    blockers: Any = None,
    no_blockers: bool = False,
) -> bool:
    return any(
        (
            dish_name is not None,
            recognition is not None,
            roles is not None,
            no_role_tags,
            blockers is not None,
            no_blockers,
        )
    )


def _json_text(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
