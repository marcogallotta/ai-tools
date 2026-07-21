"""Stage-2 command behavior for the agent-facing ``dish`` CLI."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .constants import (
    AGENT_FAMILIES,
    CHANGE_LEVELS,
    COOKING_PROJECT_GID,
    SUBMISSION_KINDS,
)
from .database import create_submission, get_submission, record_audit
from .errors import BackendFailure, DishRuleError
from .models import ResolvedRelease, SectionRegistry, agent_family, is_protocol_managed
from .results import allowed_actions_for_state, error_envelope, result_envelope
from .validation import extract_exact_label_line, validate_note


class CommandBackend(Protocol):
    def list_sections(self, project_gid: str) -> list[dict[str, Any]]: ...

    def read_task(self, task_gid: str) -> dict[str, Any]: ...

    def create_bare_task(
        self, *, title: str, project_gid: str, section_gid: str
    ) -> dict[str, Any]: ...


@dataclass
class CommandTrace:
    task_gid: str | None = None
    submission_id: str | None = None
    state: str | None = None
    known_submission: bool = False
    audit_details: dict[str, Any] = field(default_factory=dict)


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
    return {
        "protocol_release": release.version,
        "release_commit": release.commit,
        "protocol_bundle": release.bundle_for_submission(submission_kind),
        "canonical_manifest": dict(
            release.manifest_for_submission(submission_kind)
        ),
        "canonical_manifest_text": release.manifest_texts[
            "planning" if submission_kind == "planning" else "complete_task"
        ],
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


class DishApplication:
    """Command dispatcher with one audit event per invocation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        backend: CommandBackend,
        *,
        release_loader: Callable[[], ResolvedRelease],
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.release_loader = release_loader

    def execute(self, command: str, **arguments: Any) -> dict[str, Any]:
        trace = CommandTrace(
            task_gid=arguments.get("task_gid"),
            submission_id=arguments.get("submission_id"),
        )
        actor = arguments.get("agent")
        handler = getattr(self, f"_command_{command}", None)
        try:
            if handler is None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"unknown dish command: {command}",
                    rule="invalid_command",
                )
            result = handler(trace=trace, **arguments)
        except DishRuleError as exc:
            if trace.task_gid is None:
                trace.task_gid = _gid(exc.details.get("task_gid"))
            result = error_envelope(
                command,
                exc,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
            )
        except Exception:
            exc = DishRuleError(
                "INTERNAL_ERROR",
                "unexpected internal failure",
                rule="unexpected_internal_failure",
            )
            result = error_envelope(
                command,
                exc,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
            )
        self._record_invocation(command, actor, trace, result)
        return result

    def record_argument_failure(
        self,
        command: str,
        error: DishRuleError,
        *,
        agent: str | None = None,
        task_gid: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        trace = CommandTrace(task_gid=task_gid, submission_id=submission_id)
        result = error_envelope(
            command,
            error,
            task_gid=task_gid,
            submission_id=submission_id,
        )
        self._record_invocation(command, agent, trace, result)
        return result

    def _record_invocation(
        self,
        command: str,
        actor: Any,
        trace: CommandTrace,
        result: Mapping[str, Any],
    ) -> None:
        valid_actor = str(actor) if actor in AGENT_FAMILIES else None
        details = {
            "command": command,
            "ok": bool(result["ok"]),
            "code": result["code"],
            "state": result["state"],
            "retryable": bool(result["retryable"]),
            "errors": list(result["errors"]),
        }
        message = result.get("data", {}).get("message")
        if message:
            details["message"] = message
        if actor is not None and valid_actor is None:
            details["requested_agent"] = str(actor)
        details.update(trace.audit_details)
        record_audit(
            self.conn,
            submission_id=trace.submission_id if trace.known_submission else None,
            task_gid=trace.task_gid,
            event_type=f"dish.{command}",
            actor_agent=valid_actor,
            details=details,
        )

    def _command_create(
        self, *, trace: CommandTrace, agent: str, title: str
    ) -> dict[str, Any]:
        agent_family(agent)
        clean_title = _clean_required(
            title, rule="title_required", label="title"
        )
        registry = SectionRegistry.from_sections(
            self.backend.list_sections(COOKING_PROJECT_GID)
        )
        task = self.backend.create_bare_task(
            title=clean_title,
            project_gid=COOKING_PROJECT_GID,
            section_gid=registry.research_queue_gid,
        )
        task_gid = _clean_required(
            task.get("gid"), rule="created_task_gid_missing", label="created task GID"
        )
        trace.task_gid = task_gid
        trace.audit_details.update(
            {"title": clean_title, "research_queue_gid": registry.research_queue_gid}
        )
        return result_envelope(
            command="create",
            task_gid=task_gid,
            data={"task_gid": task_gid},
        )

    def _read_live_task(self, task_gid: str) -> dict[str, Any]:
        try:
            return self.backend.read_task(task_gid)
        except BackendFailure as exc:
            if exc.status == 404:
                raise DishRuleError(
                    "NOT_FOUND",
                    f"task not found: {task_gid}",
                    rule="task_not_found",
                ) from exc
            raise

    def _command_read(
        self, *, trace: CommandTrace, agent: str, task_gid: str
    ) -> dict[str, Any]:
        agent_family(agent)
        task_gid = _clean_required(
            task_gid, rule="task_gid_required", label="task GID"
        )
        trace.task_gid = task_gid
        task = self._read_live_task(task_gid)
        _require_cooking_task(task, task_gid)
        return result_envelope(
            command="read", task_gid=task_gid, data={"task": task}
        )

    def _command_inspect(
        self, *, trace: CommandTrace, agent: str, submission_id: str
    ) -> dict[str, Any]:
        agent_family(agent)
        submission_id = _clean_required(
            submission_id,
            rule="submission_id_required",
            label="submission ID",
        )
        row = get_submission(self.conn, submission_id)
        trace.submission_id = submission_id
        trace.known_submission = True
        trace.task_gid = row["task_gid"]
        trace.state = row["status"]
        protocol_bundle = _decode_json(
            row["protocol_bundle"], field_name="protocol_bundle"
        )
        canonical_manifest = _decode_json(
            row["canonical_manifest"], field_name="canonical_manifest"
        )
        allowed_actions = allowed_actions_for_state(row["status"])
        submission = {
            "submission_id": row["submission_id"],
            "task_gid": row["task_gid"],
            "kind": row["submission_kind"],
            "state": row["status"],
            "editor_agent": row["editor_agent"],
            "editor_family": row["editor_family"],
            "required_verifier_family": row["required_verifier_family"],
            "verifier_agent": row["verifier_agent"],
            "verifier_family": row["verifier_family"],
            "change_level": row["change_level"],
            "change_reason": row["change_reason"],
            "failed_verification_passes": row["failed_verification_passes"],
            "baseline_exemption_tags": (
                None
                if row["baseline_exemption_tags"] is None
                else _decode_json(
                    row["baseline_exemption_tags"],
                    field_name="baseline_exemption_tags",
                )
            ),
            "prepared_exemption_tags": (
                None
                if row["prepared_exemption_tags"] is None
                else _decode_json(
                    row["prepared_exemption_tags"],
                    field_name="prepared_exemption_tags",
                )
            ),
            "baseline_verification_line": row["baseline_verification_line"],
            "frozen_release": {
                "protocol_release": row["protocol_release"],
                "release_commit": row["release_commit"],
                "protocol_bundle": protocol_bundle,
                "canonical_manifest": canonical_manifest,
                "canonical_manifest_text": row["canonical_manifest"],
            },
            "destination": {
                "name": row["destination_section_name"],
                "gid": row["destination_section_gid"],
            },
            "completion_markers": {
                "research_queue_moved_at": row["research_queue_moved_at"],
                "notes_written_at": row["notes_written_at"],
                "destination_moved_at": row["destination_moved_at"],
                "approved_at": row["approved_at"],
                "completed_at": row["completed_at"],
            },
            "candidate_handoff": {
                "stored_by_tool": False,
                "returned_by_inspect": False,
            },
        }
        return result_envelope(
            command="inspect",
            task_gid=row["task_gid"],
            submission_id=submission_id,
            state=row["status"],
            data={
                "submission": submission,
                "legal_next_actions": allowed_actions,
            },
        )

    def _validate_start_arguments(
        self,
        *,
        kind: str,
        change_level: str | None,
        change_reason: str | None,
    ) -> tuple[str | None, str | None]:
        if kind not in SUBMISSION_KINDS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"invalid submission kind: {kind}",
                rule="invalid_submission_kind",
            )
        clean_level = str(change_level).strip() if change_level is not None else None
        clean_reason = str(change_reason).strip() if change_reason is not None else None
        if kind != "change":
            if clean_level is not None or clean_reason is not None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "change arguments are only valid for change submissions",
                    rule="change_arguments_forbidden",
                )
            return None, None
        if clean_level is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "change level is required for change submissions",
                rule="change_level_required",
            )
        if clean_level not in CHANGE_LEVELS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"invalid change level: {clean_level}",
                rule="invalid_change_level",
            )
        if not clean_reason:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "change reason is required for change submissions",
                rule="change_reason_required",
            )
        return clean_level, clean_reason

    def _command_start(
        self,
        *,
        trace: CommandTrace,
        agent: str,
        task_gid: str,
        kind: str,
        change_level: str | None = None,
        change_reason: str | None = None,
    ) -> dict[str, Any]:
        trace.audit_details.update(
            {
                "submission_kind": kind,
                "change_level": change_level,
                "change_reason": change_reason,
            }
        )
        agent_family(agent)
        task_gid = _clean_required(
            task_gid, rule="task_gid_required", label="task GID"
        )
        trace.task_gid = task_gid
        change_level, change_reason = self._validate_start_arguments(
            kind=kind,
            change_level=change_level,
            change_reason=change_reason,
        )

        task = self._read_live_task(task_gid)
        _require_cooking_task(task, task_gid)
        registry = SectionRegistry.from_sections(
            self.backend.list_sections(COOKING_PROJECT_GID)
        )
        current_section_gid = _task_section_gid(task, COOKING_PROJECT_GID)
        if not is_protocol_managed(current_section_gid, registry):
            raise DishRuleError(
                "UNMANAGED_TASK",
                f"task {task_gid} is in an excluded Cooking section",
                rule="task_in_excluded_section",
                details={"section_gid": current_section_gid},
            )

        release = self.release_loader()
        trace.audit_details.update(
            {
                "protocol_release": release.version,
                "release_commit": release.commit,
                "change_level": change_level,
                "change_reason": change_reason,
            }
        )
        notes = task.get("notes") or ""
        if not isinstance(notes, str):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task notes are malformed",
                rule="task_notes_malformed",
            )

        baseline_tags: tuple[str, ...] | None = None
        baseline_verification_line: str | None = None
        if kind == "planning":
            if notes:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "planning must start from empty notes",
                    rule="planning_notes_not_empty",
                )
        else:
            manifest_kind = "planning" if kind == "initial" else "complete_task"
            validation = validate_note(notes, release.manifests[manifest_kind])
            if not validation.ok:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "task notes do not match the required starting shape",
                    errors=validation.errors,
                )
            if validation.exemption_tags is None:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "the starting note has no parseable Exemptions value",
                    rule="invalid_exemptions",
                )
            baseline_tags = validation.exemption_tags
            if kind == "change" and change_level == "small":
                baseline_verification_line = extract_exact_label_line(
                    notes, "Verification"
                )
                if baseline_verification_line is None:
                    raise DishRuleError(
                        "VALIDATION_FAILED",
                        "small change requires an existing Verification line",
                        rule="verification_line_missing",
                    )

        bundle = release.bundle_for_submission(kind)
        manifest_key = "planning" if kind == "planning" else "complete_task"
        row = create_submission(
            self.conn,
            task_gid=task_gid,
            submission_kind=kind,
            protocol_release=release.version,
            release_commit=release.commit,
            protocol_bundle=bundle,
            canonical_manifest_text=release.manifest_texts[manifest_key],
            baseline_exemption_tags=baseline_tags,
            editor_agent=agent,
            change_level=change_level,
            change_reason=change_reason,
            baseline_verification_line=baseline_verification_line,
        )
        trace.submission_id = row["submission_id"]
        trace.known_submission = True
        trace.state = row["status"]
        frozen = _frozen_release_data(release, kind)
        return result_envelope(
            command="start",
            task_gid=task_gid,
            submission_id=row["submission_id"],
            state=row["status"],
            data={
                "submission_id": row["submission_id"],
                "frozen_release": frozen,
                "candidate_handoff": {
                    "stored_by_tool": False,
                    "author_supplies_complete_file": True,
                },
            },
        )
