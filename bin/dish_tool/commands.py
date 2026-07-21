"""Agent-facing command behavior for the guarded ``dish`` CLI."""

from __future__ import annotations

import asyncio
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
from .validation import extract_exact_label_line, validate_note


class CommandBackend(Protocol):
    def list_sections(self, project_gid: str) -> list[dict[str, Any]]: ...

    def read_task(self, task_gid: str) -> dict[str, Any]: ...

    def create_bare_task(
        self, *, title: str, project_gid: str, section_gid: str
    ) -> dict[str, Any]: ...

    def update_task_notes(self, *, task_gid: str, notes: str) -> None: ...

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
            if exc.code == "WRONG_STATE" and exc.details.get("actual"):
                trace.state = str(exc.details["actual"])
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
        self._record_invocation(command, trace.actor_agent or actor, trace, result)
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
        if command == "submit" and submission_id:
            try:
                row = get_submission(self.conn, submission_id)
            except DishRuleError:
                pass
            else:
                trace.known_submission = True
                trace.task_gid = row["task_gid"]
                trace.state = row["status"]
                trace.actor_agent = row["editor_agent"]
        result = error_envelope(
            command,
            error,
            task_gid=trace.task_gid,
            submission_id=submission_id,
            state=trace.state,
        )
        self._record_invocation(command, trace.actor_agent or agent, trace, result)
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

    def _load_candidate_file(self, file_path: str) -> str:
        from pathlib import Path

        clean_path = _clean_required(
            file_path, rule="candidate_file_required", label="candidate file"
        )
        try:
            return Path(clean_path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"candidate file not found: {clean_path}",
                rule="candidate_file_not_found",
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"candidate file could not be read: {clean_path}",
                rule="candidate_file_unreadable",
            ) from exc

    def _require_agent_workflow_available(self, row: sqlite3.Row) -> None:
        if row["status"] == "awaiting_human":
            raise DishRuleError(
                "HUMAN_ACTION_REQUIRED",
                "submission requires Human Review",
                rule="human_review_required",
            )

    def _load_verifier_submission(
        self,
        *,
        trace: CommandTrace,
        agent: str,
        submission_id: str,
    ) -> tuple[sqlite3.Row, str]:
        verifier_family = agent_family(agent)
        clean_submission_id = _clean_required(
            submission_id,
            rule="submission_id_required",
            label="submission ID",
        )
        row = get_submission(self.conn, clean_submission_id)
        trace.submission_id = clean_submission_id
        trace.known_submission = True
        trace.task_gid = row["task_gid"]
        trace.state = row["status"]
        self._require_agent_workflow_available(row)
        if row["status"] != "awaiting_verification":
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected awaiting_verification",
                rule="wrong_state",
                details={
                    "actual": row["status"],
                    "expected": ["awaiting_verification"],
                },
            )
        if verifier_family != row["required_verifier_family"]:
            raise DishRuleError(
                "VERIFIER_FAMILY_MISMATCH",
                "verifier family does not match the routed review family",
                rule="verifier_family_mismatch",
                details={
                    "expected": row["required_verifier_family"],
                    "actual": verifier_family,
                },
            )
        return row, verifier_family

    def _capture_change_diff(
        self,
        *,
        trace: CommandTrace,
        row: sqlite3.Row,
        candidate: str,
        manifest: Mapping[str, Any],
    ) -> None:
        if row["submission_kind"] != "change":
            return
        try:
            task = self._read_live_task(row["task_gid"])
        except Exception:
            trace.audit_details["change_diff_unavailable"] = (
                "live_task_read_failed"
            )
            return
        live_notes = task.get("notes")
        if not isinstance(live_notes, str):
            trace.audit_details["change_diff_unavailable"] = (
                "live_notes_malformed"
            )
            return
        try:
            trace.audit_details["change_diff"] = calculate_change_diff(
                live_notes, candidate, manifest
            )
        except Exception:
            trace.audit_details["change_diff_unavailable"] = (
                "calculation_failed"
            )

    def _prepare_move_only(
        self, *, trace: CommandTrace, row: sqlite3.Row, registry: SectionRegistry
    ) -> dict[str, Any]:
        if row["submission_kind"] == "change" and not (
            {"change_diff", "change_diff_unavailable"} & trace.audit_details.keys()
        ):
            prior_telemetry = latest_change_diff_telemetry(
                self.conn, row["submission_id"]
            )
            if prior_telemetry is not None:
                trace.audit_details.update(prior_telemetry)
        task = self._read_live_task(row["task_gid"])
        _require_cooking_task(task, row["task_gid"])
        current = _task_section_gid(task, COOKING_PROJECT_GID)
        move_needed = current == registry.research_queue_gid
        if move_needed:
            self.backend.move_task_to_section(
                task_gid=row["task_gid"], section_gid=registry.verification_queue_gid
            )
        moved_at = row["research_queue_moved_at"] or utc_now()
        final = transition_submission(
            self.conn,
            row["submission_id"],
            {"research_handoff"},
            "awaiting_verification",
            updates={"research_queue_moved_at": moved_at},
        )
        trace.state = final["status"]
        trace.audit_details.update(
            {
                "research_handoff": (
                    "moved" if move_needed else
                    "already_in_verification" if current == registry.verification_queue_gid else
                    "manual_override_preserved"
                ),
                "current_section_gid": current,
            }
        )
        return result_envelope(
            command="prepare",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data={
                "destination": {
                    "name": final["destination_section_name"],
                    "gid": final["destination_section_gid"],
                },
                "research_handoff": trace.audit_details["research_handoff"],
            },
        )

    def _command_prepare(
        self,
        *,
        trace: CommandTrace,
        agent: str,
        submission_id: str,
        file_path: str | None = None,
        exemption_revision: str | None = None,
    ) -> dict[str, Any]:
        agent_family(agent)
        submission_id = _clean_required(
            submission_id, rule="submission_id_required", label="submission ID"
        )
        row = get_submission(self.conn, submission_id)
        trace.submission_id = submission_id
        trace.known_submission = True
        trace.task_gid = row["task_gid"]
        trace.state = row["status"]

        self._require_agent_workflow_available(row)

        if row["status"] not in {"drafting", "research_handoff"}:
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected drafting or research_handoff",
                rule="wrong_state",
                details={"actual": row["status"], "expected": ["drafting", "research_handoff"]},
            )
        if agent != row["editor_agent"]:
            raise DishRuleError(
                "AGENT_MISMATCH",
                "prepare agent does not match the recorded editor",
                rule="editor_agent_mismatch",
                details={"expected": row["editor_agent"], "actual": agent},
            )

        registry = SectionRegistry.from_sections(
            self.backend.list_sections(COOKING_PROJECT_GID)
        )
        if row["status"] == "research_handoff":
            return self._prepare_move_only(trace=trace, row=row, registry=registry)

        candidate = self._load_candidate_file(file_path or "")
        manifest = _decode_json(row["canonical_manifest"], field_name="canonical_manifest")
        validation = validate_note(candidate, manifest)
        errors = list(validation.errors)

        if row["submission_kind"] == "change" and row["change_level"] == "small":
            actual_verification = extract_exact_label_line(candidate, "Verification")
            if actual_verification != row["baseline_verification_line"]:
                errors.append({
                    "rule": "verification_line_changed",
                    "expected": row["baseline_verification_line"],
                    "actual": actual_verification,
                })

        baseline_tags = (
            None if row["baseline_exemption_tags"] is None
            else tuple(_decode_json(row["baseline_exemption_tags"], field_name="baseline_exemption_tags"))
        )
        prepared_tags = validation.exemption_tags
        changed_tags = baseline_tags is not None and prepared_tags is not None and tuple(baseline_tags) != tuple(prepared_tags)
        clean_revision = str(exemption_revision or "").strip() or None

        if row["submission_kind"] == "planning":
            if clean_revision is not None:
                errors.append({"rule": "exemption_revision_forbidden"})
        elif row["change_level"] == "small":
            if changed_tags:
                errors.append({"rule": "small_change_exemptions_changed"})
            if clean_revision is not None:
                errors.append({"rule": "exemption_revision_forbidden"})
        elif changed_tags and clean_revision is None:
            errors.append({"rule": "exemption_revision_required"})
        elif not changed_tags and clean_revision is not None:
            errors.append({"rule": "exemption_revision_unnecessary"})

        if validation.destination_name and validation.destination_gid:
            try:
                destination = resolve_destination(
                    validation.destination_name, validation.destination_gid, registry
                )
            except DishRuleError as exc:
                item = {"rule": exc.rule or "destination_invalid"}
                item.update(exc.details)
                errors.append(item)
                destination = None
        else:
            destination = None

        if errors:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "candidate note failed deterministic validation",
                errors=errors,
            )
        assert destination is not None

        self._capture_change_diff(
            trace=trace, row=row, candidate=candidate, manifest=manifest
        )

        updates = {
            "prepared_exemption_tags": json.dumps(list(prepared_tags or ()), separators=(",", ":")),
            "destination_section_name": destination.name,
            "destination_section_gid": destination.gid,
            "exemption_revision": clean_revision,
        }
        needs_verifier = row["submission_kind"] == "initial" or (
            row["submission_kind"] == "change" and row["change_level"] == "large"
        )
        if not needs_verifier:
            final = transition_submission(
                self.conn, submission_id, {"drafting"}, "ready", updates=updates
            )
            trace.state = final["status"]
            return result_envelope(
                command="prepare", task_gid=row["task_gid"], submission_id=submission_id,
                state=final["status"], data={"destination": {"name": destination.name, "gid": destination.gid}}
            )

        updates["required_verifier_family"] = opposite_family(row["editor_family"])
        handoff = transition_submission(
            self.conn, submission_id, {"drafting"}, "research_handoff", updates=updates
        )
        trace.state = handoff["status"]
        return self._prepare_move_only(trace=trace, row=handoff, registry=registry)

    def _command_approve(
        self,
        *,
        trace: CommandTrace,
        agent: str,
        submission_id: str,
        file_path: str,
        correction: str,
    ) -> dict[str, Any]:
        row, verifier_family = self._load_verifier_submission(
            trace=trace,
            agent=agent,
            submission_id=submission_id,
        )
        clean_correction = _clean_required(
            correction,
            rule="correction_required",
            label="correction",
        )
        if clean_correction not in {"none", "small"}:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "correction must be none or small",
                rule="invalid_correction",
                details={"actual": clean_correction},
            )

        trace.audit_details.update(
            {
                "decision": "approve",
                "verifier_agent": agent,
                "verifier_family": verifier_family,
                "correction": clean_correction,
            }
        )
        candidate = self._load_candidate_file(file_path)
        manifest = _decode_json(
            row["canonical_manifest"], field_name="canonical_manifest"
        )
        validation = validate_note(candidate, manifest)
        errors = list(validation.errors)

        prepared_tags = tuple(
            _decode_json(
                row["prepared_exemption_tags"],
                field_name="prepared_exemption_tags",
            )
        )
        if validation.exemption_tags is not None and (
            tuple(validation.exemption_tags) != prepared_tags
        ):
            errors.append(
                {
                    "rule": "prepared_exemptions_changed",
                    "expected": list(prepared_tags),
                    "actual": list(validation.exemption_tags),
                }
            )

        destination_drift = False
        destination = None
        registry = SectionRegistry.from_sections(
            self.backend.list_sections(COOKING_PROJECT_GID)
        )
        if validation.destination_name and validation.destination_gid:
            try:
                destination = resolve_destination(
                    validation.destination_name,
                    validation.destination_gid,
                    registry,
                )
            except DishRuleError as exc:
                item = {"rule": exc.rule or "destination_invalid"}
                item.update(exc.details)
                errors.append(item)
                destination_drift = True
            else:
                if (
                    destination.name != row["destination_section_name"]
                    or destination.gid != row["destination_section_gid"]
                ):
                    errors.append(
                        {
                            "rule": "destination_changed_since_prepare",
                            "expected": {
                                "name": row["destination_section_name"],
                                "gid": row["destination_section_gid"],
                            },
                            "actual": {
                                "name": destination.name,
                                "gid": destination.gid,
                            },
                        }
                    )
                    destination_drift = True

        trace.audit_details.update(
            {
                "manifest": "complete_task",
                "validation_passed": not errors,
                "normalized_exemption_tags": (
                    None
                    if validation.exemption_tags is None
                    else list(validation.exemption_tags)
                ),
                "destination_drift": destination_drift,
            }
        )
        if destination_drift:
            final = transition_submission(
                self.conn,
                row["submission_id"],
                {"awaiting_verification"},
                "drafting",
            )
            trace.state = final["status"]
            raise DishRuleError(
                "VALIDATION_FAILED",
                "Destination changed or no longer resolves; run prepare again",
                errors=errors,
            )
        if errors:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "final note failed deterministic verification validation",
                errors=errors,
            )

        final = transition_submission(
            self.conn,
            row["submission_id"],
            {"awaiting_verification"},
            "ready",
            updates={
                "verifier_agent": agent,
                "verifier_family": verifier_family,
                "approved_at": utc_now(),
            },
        )
        trace.state = final["status"]
        return result_envelope(
            command="approve",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data={
                "verifier_agent": agent,
                "verifier_family": verifier_family,
                "correction": clean_correction,
                "destination": {
                    "name": destination.name if destination else None,
                    "gid": destination.gid if destination else None,
                },
            },
        )


    def _load_submit_submission(
        self, *, trace: CommandTrace, submission_id: str
    ) -> sqlite3.Row:
        clean_submission_id = _clean_required(
            submission_id,
            rule="submission_id_required",
            label="submission ID",
        )
        row = get_submission(self.conn, clean_submission_id)
        trace.submission_id = clean_submission_id
        trace.known_submission = True
        trace.task_gid = row["task_gid"]
        trace.state = row["status"]
        trace.actor_agent = row["editor_agent"]
        if row["status"] not in {"ready", "written"}:
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected ready or written",
                rule="wrong_state",
                details={
                    "actual": row["status"],
                    "expected": ["ready", "written"],
                },
            )
        return row

    def _refresh_trace_state(self, trace: CommandTrace) -> None:
        if not trace.submission_id:
            return
        try:
            trace.state = get_submission(self.conn, trace.submission_id)["status"]
        except DishRuleError:
            pass

    def _write_notes_once(
        self,
        *,
        trace: CommandTrace,
        row: sqlite3.Row,
        candidate: str,
    ) -> sqlite3.Row:
        attempt = begin_write_attempt(self.conn, row["submission_id"])
        trace.state = "in_flight"
        trace.audit_details.update(
            {
                "write_attempt_id": attempt.attempt_id,
                "write_outcome": "attempted",
            }
        )
        try:
            self.backend.update_task_notes(
                task_gid=row["task_gid"], notes=candidate
            )
        except BackendFailure as exc:
            target = (
                "ready" if exc.code == "BACKEND_REJECTED" else "uncertain"
            )
            try:
                final = finish_write_attempt(
                    self.conn,
                    row["submission_id"],
                    attempt_id=attempt.attempt_id,
                    target_state=target,
                )
            except DishRuleError:
                self._refresh_trace_state(trace)
                raise
            trace.state = final["status"]
            trace.audit_details["write_outcome"] = (
                "confirmed_non_application"
                if target == "ready"
                else "uncertain"
            )
            raise
        except (Exception, asyncio.CancelledError) as exc:
            uncertain = BackendFailure(
                "BACKEND_UNCERTAIN",
                f"notes write outcome is unknown: {exc}",
                retryable=False,
                details={"exception_type": type(exc).__name__},
            )
            try:
                final = finish_write_attempt(
                    self.conn,
                    row["submission_id"],
                    attempt_id=attempt.attempt_id,
                    target_state="uncertain",
                )
            except DishRuleError:
                self._refresh_trace_state(trace)
                raise
            trace.state = final["status"]
            trace.audit_details["write_outcome"] = "uncertain"
            raise uncertain from exc

        try:
            final = finish_write_attempt(
                self.conn,
                row["submission_id"],
                attempt_id=attempt.attempt_id,
                target_state="written",
            )
        except DishRuleError:
            self._refresh_trace_state(trace)
            raise
        trace.state = final["status"]
        trace.audit_details["write_outcome"] = "confirmed_success"
        return final

    def _complete_destination_handoff(
        self, *, trace: CommandTrace, row: sqlite3.Row
    ) -> dict[str, Any]:
        registry = SectionRegistry.from_sections(
            self.backend.list_sections(COOKING_PROJECT_GID)
        )
        destination_name = str(row["destination_section_name"] or "").strip()
        destination_gid = str(row["destination_section_gid"] or "").strip()
        if not destination_name or not destination_gid:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "written submission is missing its validated Destination",
                rule="stored_destination_missing",
            )
        destination = resolve_destination(
            destination_name, destination_gid, registry
        )
        task = self._read_live_task(row["task_gid"])
        _require_cooking_task(task, row["task_gid"])
        current = _task_section_gid(task, COOKING_PROJECT_GID)
        if current is None:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task has no resolvable Cooking section for destination handoff",
                rule="task_section_unresolved",
            )

        moved = False
        if current == registry.verification_queue_gid:
            self.backend.move_task_to_section(
                task_gid=row["task_gid"], section_gid=destination.gid
            )
            handoff = "moved_to_destination"
            moved = True
        elif current == destination.gid:
            handoff = "already_at_destination"
        elif (
            current == registry.research_queue_gid
            and row["submission_kind"] == "planning"
        ):
            handoff = "planning_research_queue"
        elif current not in registry.queue_gids:
            handoff = "manual_override_preserved"
        else:
            raise DishRuleError(
                "CONFLICT",
                "non-planning submission remains in Research Queue",
                rule="unexpected_research_queue_position",
                details={"current_section_gid": current},
            )

        completed_at = utc_now()
        updates: dict[str, Any] = {"completed_at": completed_at}
        if moved:
            updates["destination_moved_at"] = completed_at
        final = transition_submission(
            self.conn,
            row["submission_id"],
            {"written"},
            "consumed",
            updates=updates,
        )
        trace.state = final["status"]
        trace.audit_details["handoff"] = handoff
        trace.audit_details["current_section_gid"] = current
        return result_envelope(
            command="submit",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data={
                "write_outcome": trace.audit_details.get(
                    "write_outcome", "already_written"
                ),
                "handoff": handoff,
                "destination": {
                    "name": destination.name,
                    "gid": destination.gid,
                },
            },
        )

    def _command_submit(
        self,
        *,
        trace: CommandTrace,
        submission_id: str,
        file_path: str,
    ) -> dict[str, Any]:
        row = self._load_submit_submission(
            trace=trace, submission_id=submission_id
        )
        trace.audit_details["write_outcome"] = "already_written"
        if row["status"] == "ready":
            candidate = self._load_candidate_file(file_path)
            row = self._write_notes_once(
                trace=trace, row=row, candidate=candidate
            )
        return self._complete_destination_handoff(trace=trace, row=row)

    def _command_reject(
        self,
        *,
        trace: CommandTrace,
        agent: str,
        submission_id: str,
        reason: str,
        changed_since_prior: str | None = None,
        take_ownership: bool = False,
    ) -> dict[str, Any]:
        row, verifier_family = self._load_verifier_submission(
            trace=trace,
            agent=agent,
            submission_id=submission_id,
        )
        clean_reason = _clean_required(
            reason,
            rule="rejection_reason_required",
            label="rejection reason",
        )
        clean_changed = str(changed_since_prior or "").strip() or None
        current_passes = int(row["failed_verification_passes"])
        if current_passes not in {0, 1}:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "stored failed-verification counter is invalid for routed review",
                rule="invalid_failed_verification_counter",
                details={"actual": current_passes},
            )
        next_passes = current_passes + 1
        if current_passes >= 1 and clean_changed is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "the second rejection requires what changed since the prior pass",
                rule="changed_since_prior_required",
            )

        updates: dict[str, Any] = {
            "failed_verification_passes": next_passes,
        }
        if take_ownership:
            updates.update(
                {
                    "editor_agent": agent,
                    "editor_family": verifier_family,
                }
            )
        target_state = "drafting" if next_passes == 1 else "awaiting_human"

        trace.audit_details.update(
            {
                "decision": "reject",
                "verifier_agent": agent,
                "verifier_family": verifier_family,
                "reason": clean_reason,
                "changed_since_prior": clean_changed,
                "take_ownership": bool(take_ownership),
                "failed_verification_passes": next_passes,
            }
        )
        if target_state == "awaiting_human":
            first_reason = latest_successful_rejection_reason(
                self.conn, row["submission_id"]
            )
            if first_reason is None:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "prior successful rejection reason is missing",
                    rule="prior_rejection_reason_missing",
                )
            trace.audit_details["escalation_summary"] = {
                "first_rejection_reason": first_reason,
                "second_rejection_reason": clean_reason,
                "changed_since_prior": clean_changed,
            }

        final = transition_submission(
            self.conn,
            row["submission_id"],
            {"awaiting_verification"},
            target_state,
            updates=updates,
        )
        trace.state = final["status"]
        data = {
            "decision": "reject",
            "failed_verification_passes": next_passes,
            "take_ownership": bool(take_ownership),
        }
        if target_state == "awaiting_human":
            data["escalation_summary"] = trace.audit_details["escalation_summary"]
            return result_envelope(
                command="reject",
                ok=False,
                code="HUMAN_ACTION_REQUIRED",
                task_gid=row["task_gid"],
                submission_id=row["submission_id"],
                state=final["status"],
                data=data,
                errors=[{"rule": "human_review_required"}],
            )
        return result_envelope(
            command="reject",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data=data,
        )
