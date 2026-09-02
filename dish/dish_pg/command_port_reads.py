"""Read and semantic-proposal helpers for the PostgreSQL command port."""
from __future__ import annotations

import uuid
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select

from . import models
from . import stage3_models as wf
from .command_contract import definition_for
from .command_port_common import (
    SEMANTIC_PROPOSAL_PREFIX as _SEMANTIC_PROPOSAL_PREFIX,
    CommandCall,
    CommandResult,
    CommandRuleError,
    decode_semantic_proposal_text as _decode_semantic_proposal_text,
    json_safe as _json_safe,
    task_reference_from_dish as _task_reference_from_dish,
)
from .document_authority import (
    CanonicalDocumentError,
    parse_canonical_document,
)
from .read_model import ReadModelError
from .planner import AuthorityFence, AuthoritativeSnapshot
from .repositories import (
    CoreAuthorityError,
    RegistryRepository,
)
from dish_tool.content_versions import content_identity
from dish_tool.governed_diff import (
    agent_attested_decision_appends,
    canonical_diff,
    governed_changes_requiring_authorization,
    validate_semantic_proposal,
)
from dish_tool.errors import DishRuleError


class PostgresCommandReadMixin:
    """Read/planning helpers sharing the caller-owned port session."""

    def _execute_read(self, call: CommandCall) -> CommandResult:
        if call.request_id is not None:
            raise CommandRuleError(
                "REQUEST_ID_NOT_ALLOWED", "read-only commands do not accept request_id", http_status=400
            )
        task = None
        operation = None
        if call.command_name == "sections":
            data: Mapping[str, Any] = {"sections": self.reads.sections()}
        elif call.command_name == "section-tasks":
            reference = call.arguments.get("section_id") or call.arguments.get("section_gid")
            if reference is None:
                raise CommandRuleError("SECTION_REQUIRED", "section reference is required", http_status=400)
            try:
                self.reads.resolve_section(str(reference))
            except ReadModelError as exc:
                raise CommandRuleError(
                    "SECTION_NOT_FOUND",
                    str(exc),
                    http_status=404,
                    data={"section_reference": str(reference)},
                ) from exc
            page = self.reads.section_tasks(
                section_reference=str(reference),
                cursor=call.arguments.get("cursor"),
                page_size=int(call.arguments.get("page_size", 50)),
            )
            data = {
                "tasks": [
                    asdict(item)
                    | {
                        "dish_id": str(item.task_id),
                        "task_id": str(item.task_id),
                        "task_gid": item.external_task_id,
                        "section_id": str(item.section_id),
                    }
                    for item in page.items
                ],
                "next_cursor": page.next_cursor,
                "registry_version_id": str(page.registry_version_id),
                "registry_revision": page.registry_revision,
            }
        elif call.command_name == "proposals":
            data = self._proposals()
        elif call.command_name == "queue":
            if call.principal_class != "admin":
                raise CommandRuleError(
                    "PRINCIPAL_SCOPE_MISMATCH",
                    "queue is available only on the private admin surface",
                    http_status=403,
                )
            data = self._queue()
        elif call.command_name == "cook-logs":
            reference = call.arguments.get("dish_id")
            if reference is None:
                raise CommandRuleError("TASK_REQUIRED", "dish_id is required", http_status=400)
            try:
                task = self.reads.resolve_task(str(reference))
            except ReadModelError as exc:
                raise CommandRuleError("TASK_NOT_FOUND", str(exc), http_status=404) from exc
            generation_id = self.reads.active_generation().generation_id
            page_size = int(call.arguments.get("page_size", 50))
            cursor = call.arguments.get("cursor")
            after_time = None
            after_id = None
            if cursor is not None:
                try:
                    payload = self.reads.cursor_codec.decode(str(cursor))
                    if (
                        payload.get("kind") != "cook_logs"
                        or payload.get("generation_id") != str(generation_id)
                        or payload.get("task_id") != str(task.task_id)
                    ):
                        raise ValueError("cursor scope mismatch")
                    after_time = datetime.fromisoformat(str(payload["recorded_at"]))
                    after_id = uuid.UUID(str(payload["log_id"]))
                except (KeyError, TypeError, ValueError, ReadModelError) as exc:
                    raise CommandRuleError("INVALID_CURSOR", "cook-log cursor is invalid", http_status=400) from exc
            statement = (
                select(wf.CookLogEntry, wf.CommandExecution, wf.ServiceRequest)
                .join(
                    wf.CommandExecution,
                    wf.CommandExecution.execution_id
                    == wf.CookLogEntry.command_execution_id,
                )
                .join(
                    wf.ServiceRequest,
                    wf.ServiceRequest.request_id == wf.CommandExecution.request_id,
                )
                .where(
                    wf.CookLogEntry.generation_id == generation_id,
                    wf.CookLogEntry.task_id == task.task_id,
                )
            )
            if after_time is not None and after_id is not None:
                statement = statement.where(
                    (wf.CookLogEntry.recorded_at > after_time)
                    | (
                        (wf.CookLogEntry.recorded_at == after_time)
                        & (wf.CookLogEntry.log_id > after_id)
                    )
                )
            rows = list(
                self.session.execute(
                    statement.order_by(
                        wf.CookLogEntry.recorded_at, wf.CookLogEntry.log_id
                    ).limit(page_size + 1)
                )
            )
            visible = rows[:page_size]
            next_cursor = None
            if len(rows) > page_size:
                last = visible[-1][0]
                next_cursor = self.reads.cursor_codec.encode({
                    "kind": "cook_logs",
                    "generation_id": str(generation_id),
                    "task_id": str(task.task_id),
                    "recorded_at": last.recorded_at.isoformat(),
                    "log_id": str(last.log_id),
                })
            data = {
                "dish_id": str(task.task_id),
                "logs": [
                    {
                        "log_id": str(entry.log_id),
                        "text": entry.text,
                        "recorded_at": entry.recorded_at.isoformat(),
                        "content_version_id": str(entry.content_version_id),
                        "dish_version": entry.dish_version,
                        "command_execution_id": str(execution.execution_id),
                        "request_id": str(request.request_id),
                        "run_id": str(request.run_id),
                        "owner_id": request.owner_id,
                        "principal_class": request.principal_class,
                    }
                    for entry, execution, request in visible
                ],
                "next_cursor": next_cursor,
            }
        elif call.command_name == "attention":
            if call.principal_class != "admin":
                raise CommandRuleError(
                    "PRINCIPAL_SCOPE_MISMATCH",
                    "attention is available only on the private admin surface",
                    http_status=403,
                )
            data = self._attention()
        elif call.command_name == "holds":
            if call.principal_class != "admin":
                raise CommandRuleError(
                    "PRINCIPAL_SCOPE_MISMATCH",
                    "holds is available only on the private admin surface",
                    http_status=403,
                )
            data = self._holds()
        elif call.command_name == "read":
            reference = (
                call.arguments.get("dish_id")
                or call.arguments.get("task_id")
                or call.arguments.get("task_gid")
            )
            if reference is None:
                raise CommandRuleError("TASK_REQUIRED", "task reference is required", http_status=400)
            try:
                task = self.reads.resolve_task(str(reference))
            except ReadModelError as exc:
                raise CommandRuleError(
                    "DISH_NOT_FOUND",
                    str(exc),
                    http_status=404,
                    data={"dish_reference": str(reference)},
                ) from exc
            view = self.reads.task_view(task.task_id)
            if view.operation_id is not None:
                operation = self.session.get(wf.WorkflowOperation, view.operation_id)
            task_gid = self.session.scalar(
                select(models.TaskExternalAlias.external_id).where(
                    models.TaskExternalAlias.task_id == view.task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            freshness = dict(view.projection_freshness)
            freshness = dict(self.projection_recorder.task_freshness(view.task_id))
            data = asdict(view) | {
                "dish_id": str(view.task_id),
                "task_id": str(view.task_id),
                "content_version_id": str(view.content_version_id),
                "section_id": str(view.section_id),
                "operation_id": str(view.operation_id) if view.operation_id else None,
                "projection_freshness": freshness,
                "identity_binding": {
                    "dish_id": str(view.task_id),
                    "task_gid": task_gid,
                },
            }
        else:
            raise CommandRuleError("NOT_A_QUERY", "command is not a read query")
        if task is not None:
            data, envelope = self._continuation_envelope(
                call=call,
                task=task,
                operation=operation,
                data=data,
            )
            return self._command_result(
                True, call.command_name, "OK", 200, data, envelope=envelope
            )
        return CommandResult(True, call.command_name, "OK", 200, data)


    def _semantic_proposal_requirement(
        self, proposal_id: str | uuid.UUID, *, lock: bool = False
    ) -> wf.HumanReviewRequirement:
        try:
            proposal_uuid = uuid.UUID(str(proposal_id))
        except ValueError as exc:
            raise CommandRuleError(
                "INVALID_PROPOSAL_ID",
                "semantic proposal identifier must be a UUID",
                http_status=400,
            ) from exc
        statement = select(wf.HumanReviewRequirement).where(
            wf.HumanReviewRequirement.requirement_id == proposal_uuid
        )
        if lock and self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        requirement = self.session.scalar(
            statement.execution_options(populate_existing=True)
        )
        if requirement is None or requirement.state != "open":
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_NOT_FOUND",
                "semantic proposal is missing, closed, or stale",
                http_status=404,
            )
        if _decode_semantic_proposal_text(requirement.question) is None:
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_NOT_FOUND",
                "the requested Human Review row is not a semantic proposal",
                http_status=404,
            )
        return requirement

    def _semantic_proposal_payload(
        self, requirement: wf.HumanReviewRequirement
    ) -> dict[str, Any]:
        payload = _decode_semantic_proposal_text(requirement.question)
        if payload is None:
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "stored requirement is not a PostgreSQL semantic proposal",
            )
        if payload.get("proposal_id") != str(requirement.requirement_id):
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "stored proposal identity does not match its durable requirement row",
            )
        return payload

    def _semantic_proposal_bundle(
        self,
        *,
        proposal_id: uuid.UUID,
        before: Any,
        after: Any,
        reason: str,
        source_cycle_id: uuid.UUID,
        governed_change_fields: Any,
    ) -> dict[str, Any] | None:
        declared = {
            str(value)
            for value in (governed_change_fields or ())
            if str(value).strip()
        }
        if not declared:
            return None
        try:
            validate_semantic_proposal(before, after)
            attested = (
                agent_attested_decision_appends(before, after)
                if "Decisions" in declared
                else ()
            )
            required = governed_changes_requiring_authorization(
                before,
                after,
                agent_attested_decisions=attested,
            )
        except DishRuleError as exc:
            raise CommandRuleError(
                str(exc.code),
                str(exc),
                http_status=409,
                data=dict(getattr(exc, "details", {}) or {}),
            ) from exc
        if not required:
            return None
        required_fields = {change.field for change in required}
        if not required_fields.issubset(declared):
            raise CommandRuleError(
                "GOVERNED_CHANGE_FIELDS_INCOMPLETE",
                "governed_change_fields must name every governed field changed by the semantic proposal",
                http_status=409,
                data={"missing_fields": sorted(required_fields - declared)},
            )
        linked = canonical_diff(before, after)
        rendered = after.render().splitlines()
        candidate_title = rendered[0]
        candidate_body = "\n".join(rendered[1:]) + "\n"
        candidate_identity = content_identity(candidate_title, candidate_body)
        return {
            "version": 1,
            "proposal_id": str(proposal_id),
            "proposal_class": "large",
            "source_cycle_id": str(source_cycle_id),
            "reason": reason,
            "candidate": {
                "title": candidate_title,
                "body": candidate_body,
                "identity": candidate_identity,
            },
            "agent_attested_decisions": list(attested),
            "required_authorizations": [
                {
                    "field": change.field,
                    "before": _json_safe(change.before),
                    "after": _json_safe(change.after),
                }
                for change in required
            ],
            "linked_changes": [
                {"path": path, "before": old, "after": new}
                for path, (old, new) in sorted(linked.items())
            ],
        }

    def _validate_semantic_proposal_requirement(
        self,
        requirement: wf.HumanReviewRequirement,
        *,
        require_current: bool = True,
    ) -> tuple[dict[str, Any], Any, list[dict[str, Any]]]:
        payload = self._semantic_proposal_payload(requirement)
        operation = self.session.get(wf.WorkflowOperation, requirement.operation_id)
        baseline = self.session.get(
            models.ContentVersion, requirement.baseline_content_version_id
        )
        if (
            operation is None
            or baseline is None
            or operation.generation_id != requirement.generation_id
            or operation.task_id != requirement.task_id
            or operation.lifecycle != "open"
            or operation.phase != "held_human"
        ):
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_STALE",
                "proposal no longer belongs to the exact open PostgreSQL workflow occurrence",
            )
        if requirement.cycle_id is None or payload.get("source_cycle_id") != str(
            requirement.cycle_id
        ):
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "proposal source cycle identity is incomplete",
            )
        if require_current and self._current_content_version_id(
            requirement.generation_id, requirement.task_id
        ) != requirement.baseline_content_version_id:
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_STALE",
                "proposal baseline is no longer the current canonical Dish content",
            )
        candidate = payload.get("candidate")
        if not isinstance(candidate, dict):
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "proposal candidate bundle is missing",
            )
        title = candidate.get("title")
        body = candidate.get("body")
        identity = candidate.get("identity")
        if not isinstance(title, str) or not isinstance(body, str) or not isinstance(identity, str):
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "proposal candidate bundle is malformed",
            )
        observed_identity = content_identity(title, body)
        if observed_identity != identity:
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "proposal candidate identity does not match the stored bytes",
            )
        before_parts = parse_canonical_document(
            title=baseline.title,
            body=baseline.body,
            expected_status="pending-verification",
        )
        candidate_parts = parse_canonical_document(
            title=title,
            body=body,
            expected_status="pending-verification",
        )
        try:
            validate_semantic_proposal(before_parts.document, candidate_parts.document)
            attested = tuple(payload.get("agent_attested_decisions") or ())
            required = governed_changes_requiring_authorization(
                before_parts.document,
                candidate_parts.document,
                agent_attested_decisions=attested,
            )
        except DishRuleError as exc:
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                str(exc),
                data=dict(getattr(exc, "details", {}) or {}),
            ) from exc
        expected_required = [
            {
                "field": change.field,
                "before": _json_safe(change.before),
                "after": _json_safe(change.after),
            }
            for change in required
        ]
        stored_required = payload.get("required_authorizations")
        if _json_safe(stored_required) != _json_safe(expected_required) or not expected_required:
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "proposal authorization bundle does not match the exact candidate diff",
            )
        expected_linked = [
            {"path": path, "before": old, "after": new}
            for path, (old, new) in sorted(
                canonical_diff(before_parts.document, candidate_parts.document).items()
            )
        ]
        if _json_safe(payload.get("linked_changes")) != _json_safe(expected_linked):
            raise CommandRuleError(
                "SEMANTIC_PROPOSAL_INTEGRITY_FAILED",
                "proposal linked-change bundle does not match the exact candidate diff",
            )
        return payload, candidate_parts, expected_required

    def _available_governed_change_grants(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        operation_id: uuid.UUID,
        required: list[dict[str, Any]],
    ) -> list[tuple[wf.MarcoAuthorizationGrant, wf.MarcoAuthorizationState]]:
        """Match exact available Marco grants for one governed candidate diff."""
        rows = list(
            self.session.execute(
                select(wf.MarcoAuthorizationGrant, wf.MarcoAuthorizationState)
                .join(
                    wf.MarcoAuthorizationState,
                    wf.MarcoAuthorizationState.grant_id
                    == wf.MarcoAuthorizationGrant.grant_id,
                )
                .where(
                    wf.MarcoAuthorizationGrant.generation_id == generation_id,
                    wf.MarcoAuthorizationGrant.task_id == task_id,
                    wf.MarcoAuthorizationState.state == "available",
                )
            ).all()
        )
        available = [
            row for row in rows if row[0].operation_id in {None, operation_id}
        ]
        # Prefer a grant bound to this exact operation over a task-wide grant.
        available.sort(key=lambda row: row[0].operation_id is None)
        matched: list[tuple[wf.MarcoAuthorizationGrant, wf.MarcoAuthorizationState]] = []
        used: set[uuid.UUID] = set()
        for change in required:
            found = None
            for grant, state in available:
                if grant.grant_id in used:
                    continue
                if (
                    grant.field_name == change["field"]
                    and _json_safe(grant.before_value) == _json_safe(change["before"])
                    and _json_safe(grant.after_value) == _json_safe(change["after"])
                ):
                    found = (grant, state)
                    break
            if found is None:
                return []
            used.add(found[0].grant_id)
            matched.append(found)
        return matched

    def _available_semantic_proposal_grants(
        self,
        requirement: wf.HumanReviewRequirement,
        required: list[dict[str, Any]],
    ) -> list[tuple[wf.MarcoAuthorizationGrant, wf.MarcoAuthorizationState]]:
        return self._available_governed_change_grants(
            generation_id=requirement.generation_id,
            task_id=requirement.task_id,
            operation_id=requirement.operation_id,
            required=required,
        )

    def _semantic_proposal_status(
        self, operation: wf.WorkflowOperation
    ) -> tuple[str | None, bool]:
        requirement = self.session.scalar(
            select(wf.HumanReviewRequirement)
            .where(
                wf.HumanReviewRequirement.operation_id == operation.operation_id,
                wf.HumanReviewRequirement.state == "open",
            )
            .order_by(
                wf.HumanReviewRequirement.opened_at.desc(),
                wf.HumanReviewRequirement.requirement_id.desc(),
            )
            .limit(1)
        )
        if requirement is None or not requirement.question.startswith(
            _SEMANTIC_PROPOSAL_PREFIX
        ):
            return None, False
        try:
            _payload, _candidate, required = self._validate_semantic_proposal_requirement(
                requirement
            )
        except CommandRuleError:
            return "pending", False
        grants = self._available_semantic_proposal_grants(requirement, required)
        return ("approved", True) if grants else ("pending", False)

    def _proposals(self) -> Mapping[str, Any]:
        generation_id = self.reads.active_generation().generation_id
        requirements = list(
            self.session.scalars(
                select(wf.HumanReviewRequirement)
                .where(
                    wf.HumanReviewRequirement.generation_id == generation_id,
                    wf.HumanReviewRequirement.state == "open",
                )
                .order_by(
                    wf.HumanReviewRequirement.opened_at,
                    wf.HumanReviewRequirement.requirement_id,
                )
            )
        )
        rows: list[dict[str, Any]] = []
        for requirement in requirements:
            if not requirement.question.startswith(
                _SEMANTIC_PROPOSAL_PREFIX
            ):
                continue
            try:
                payload, _candidate, required = self._validate_semantic_proposal_requirement(
                    requirement
                )
            except CommandRuleError:
                continue
            grants = self._available_semantic_proposal_grants(requirement, required)
            if not grants:
                continue
            rows.append(
                {
                    "proposal_id": str(requirement.requirement_id),
                    "dish_id": str(requirement.task_id),
                    "task_id": str(requirement.task_id),
                    "operation_id": str(requirement.operation_id),
                    "cycle_id": str(requirement.cycle_id),
                    "candidate_identity": payload["candidate"]["identity"],
                    "reason": payload.get("reason"),
                    "required_authorizations": required,
                    "agent_action": {
                        "command": "apply-proposal",
                        "arguments": {
                            "proposal_id": str(requirement.requirement_id)
                        },
                    },
                }
            )
        return {
            "count": len(rows),
            "proposals": rows,
            "instruction": (
                "Claim and apply an approved PostgreSQL proposal exactly as stored; "
                "do not reconstruct or edit its candidate."
            ),
        }

    def _attention(self) -> Mapping[str, Any]:
        """Return a conservative, read-only global workflow attention view."""

        generation = self.reads.active_generation()
        now = datetime.now().astimezone()
        operations = self.session.scalars(
            select(wf.WorkflowOperation)
            .where(
                wf.WorkflowOperation.generation_id == generation.generation_id,
                wf.WorkflowOperation.lifecycle == "open",
            )
            .order_by(wf.WorkflowOperation.created_at, wf.WorkflowOperation.operation_id)
        ).all()
        items: list[dict[str, Any]] = []
        healthy_count = 0
        counts = {"safe_cleanup": 0, "multi_step_safe": 0, "needs_marco": 0, "unsafe": 0}
        for operation in operations:
            lease = self.session.scalar(
                select(wf.ServiceLease)
                .where(
                    wf.ServiceLease.operation_id == operation.operation_id,
                    wf.ServiceLease.lease_kind == "actor",
                    wf.ServiceLease.state == "active",
                )
                .order_by(wf.ServiceLease.issued_at.desc())
                .limit(1)
            )
            abandonment = self.session.scalar(
                select(wf.AbandonmentAttempt)
                .where(
                    wf.AbandonmentAttempt.source_operation_id == operation.operation_id,
                    wf.AbandonmentAttempt.state.in_(("preparing", "published", "blocked", "reconciling")),
                )
                .order_by(wf.AbandonmentAttempt.created_at.desc())
                .limit(1)
            )
            uncertain_execution = self.session.scalar(
                select(wf.CommandExecution.execution_id)
                .join(
                    wf.OperationExecutionFence,
                    wf.OperationExecutionFence.execution_id == wf.CommandExecution.execution_id,
                )
                .where(
                    wf.OperationExecutionFence.operation_id == operation.operation_id,
                    wf.CommandExecution.status == "uncertain",
                )
                .limit(1)
            )
            category: str | None = None
            reason: str | None = None
            if uncertain_execution is not None:
                category, reason = "unsafe", "an execution outcome is uncertain"
            elif abandonment is not None:
                if abandonment.state == "blocked":
                    category, reason = "unsafe", "abandonment is blocked at an unsupported frontier"
                elif abandonment.state == "published":
                    category, reason = "multi_step_safe", "an abandonment successor is waiting for continuation"
                else:
                    category, reason = "multi_step_safe", "abandonment has a deterministic continuation"
            elif operation.phase in {"held_evidence", "held_human"}:
                category, reason = "needs_marco", "the workflow is waiting for Marco"
            elif lease is not None and lease.expires_at <= now:
                category, reason = "multi_step_safe", "the actor lease has expired"
            elif lease is not None:
                healthy_count += 1
                continue
            else:
                healthy_count += 1
                continue
            counts[category] += 1
            state = self.session.get(models.DishState, (generation.generation_id, operation.task_id))
            version = self.session.get(models.ContentVersion, state.current_content_version_id) if state else None
            items.append({
                "category": category,
                "category_reason": reason,
                "task_id": str(operation.task_id),
                "task_title": version.title if version is not None else None,
                "operation_id": str(operation.operation_id),
                "phase": operation.phase,
                "lease_id": str(lease.lease_id) if lease is not None else None,
                "lease_expires_at": lease.expires_at.isoformat() if lease is not None else None,
                "abandonment_id": str(abandonment.abandonment_id) if abandonment is not None else None,
            })
        return {
            "checked_count": len(operations),
            "attention_count": len(items),
            "healthy_count": healthy_count,
            "category_counts": counts,
            "attention_items": items,
            "read_only": True,
        }

    def _queue(self) -> Mapping[str, Any]:
        """Return Marco's PostgreSQL-native queue from canonical workflow facts."""

        generation = self.reads.active_generation()
        now = datetime.now().astimezone()
        operations = list(
            self.session.scalars(
                select(wf.WorkflowOperation)
                .where(
                    wf.WorkflowOperation.generation_id == generation.generation_id,
                    wf.WorkflowOperation.lifecycle == "open",
                )
                .order_by(
                    wf.WorkflowOperation.created_at,
                    wf.WorkflowOperation.operation_id,
                )
            )
        )
        items: list[dict[str, Any]] = []
        category_counts = {"system": 0, "needs_marco": 0, "unsafe": 0}
        group_order = {
            "human_review": 0,
            "evidence": 1,
            "change_review": 2,
            "recovery": 3,
            "system": 4,
        }

        for operation in operations:
            state = self.session.get(
                models.DishState,
                (generation.generation_id, operation.task_id),
            )
            version = (
                self.session.get(models.ContentVersion, state.current_content_version_id)
                if state is not None
                else None
            )
            task_gid = self.session.scalar(
                select(models.TaskExternalAlias.external_id).where(
                    models.TaskExternalAlias.task_id == operation.task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            signals: list[dict[str, Any]] = []

            def signal(
                *,
                kind: str,
                category: str,
                summary: str,
                detail: str | None = None,
                action: Mapping[str, Any] | None = None,
            ) -> None:
                row: dict[str, Any] = {
                    "kind": kind,
                    "category": category,
                    "summary": summary,
                }
                if detail:
                    row["detail"] = detail
                if action is not None:
                    row["queue_action"] = dict(action)
                signals.append(row)

            executions = list(
                self.session.scalars(
                    select(wf.CommandExecution).where(
                        wf.CommandExecution.generation_id == generation.generation_id,
                        wf.CommandExecution.operation_id == operation.operation_id,
                        wf.CommandExecution.status.in_(("pending", "claimed", "uncertain")),
                    )
                )
            )
            if any(execution.status == "uncertain" for execution in executions):
                signal(
                    kind="uncertain_execution",
                    category="unsafe",
                    summary="A command outcome is uncertain and requires reconciliation.",
                )
            elif any(
                execution.status == "pending"
                or (
                    execution.status == "claimed"
                    and execution.claim_expires_at is not None
                    and execution.claim_expires_at <= now
                )
                for execution in executions
            ):
                signal(
                    kind="orphaned_execution",
                    category="unsafe",
                    summary="An unfinished command execution has no live claim.",
                )

            abandonment = self.session.scalar(
                select(wf.AbandonmentAttempt)
                .where(
                    wf.AbandonmentAttempt.source_operation_id == operation.operation_id,
                    wf.AbandonmentAttempt.state.in_(
                        ("preparing", "published", "blocked", "reconciling")
                    ),
                )
                .order_by(
                    wf.AbandonmentAttempt.created_at.desc(),
                    wf.AbandonmentAttempt.abandonment_id.desc(),
                )
                .limit(1)
            )
            if abandonment is not None:
                signal(
                    kind="blocked_abandonment" if abandonment.state == "blocked" else "abandonment_continuation",
                    category="unsafe" if abandonment.state == "blocked" else "system",
                    summary=(
                        "Abandonment is blocked at an unsupported frontier."
                        if abandonment.state == "blocked"
                        else "Abandonment has a deterministic system continuation."
                    ),
                    action=(
                        {
                            "kind": "inspect",
                            "dish_id": str(operation.task_id),
                            "operation_id": str(operation.operation_id),
                            "abandonment_id": str(abandonment.abandonment_id),
                        }
                        if abandonment.state == "blocked"
                        else None
                    ),
                )

            latest_cycle = self.session.scalar(
                select(wf.VerificationCycle)
                .where(wf.VerificationCycle.operation_id == operation.operation_id)
                .order_by(
                    wf.VerificationCycle.cycle_sequence.desc(),
                    wf.VerificationCycle.cycle_id.desc(),
                )
                .limit(1)
            )
            open_hold = self.session.scalar(
                select(wf.EvidenceHold)
                .where(
                    wf.EvidenceHold.operation_id == operation.operation_id,
                    wf.EvidenceHold.state == "open",
                )
                .order_by(wf.EvidenceHold.opened_at.desc())
                .limit(1)
            )
            requirement = self.session.scalar(
                select(wf.HumanReviewRequirement)
                .where(
                    wf.HumanReviewRequirement.operation_id == operation.operation_id,
                    wf.HumanReviewRequirement.state == "open",
                )
                .order_by(
                    wf.HumanReviewRequirement.opened_at.desc(),
                    wf.HumanReviewRequirement.requirement_id.desc(),
                )
                .limit(1)
            )

            if operation.phase == "held_evidence" and open_hold is not None:
                resume_status = (
                    "pending-research"
                    if open_hold.cycle_id is None
                    else "pending-verification"
                )
                signal(
                    kind="evidence_hold",
                    category="needs_marco",
                    summary="Dish is waiting for Marco-supplied evidence.",
                    detail=open_hold.reason,
                    action={
                        "kind": "supply_evidence",
                        "dish_id": str(operation.task_id),
                        "operation_id": str(operation.operation_id),
                        "hold_id": str(open_hold.hold_id),
                        "cycle_id": str(open_hold.cycle_id) if open_hold.cycle_id else None,
                        "hold_identity": version.content_identity if version is not None else None,
                        "resume_status": resume_status,
                    },
                )
            elif operation.phase == "held_human" and requirement is not None:
                if requirement.question.startswith(_SEMANTIC_PROPOSAL_PREFIX):
                    try:
                        payload, _candidate, required = self._validate_semantic_proposal_requirement(
                            requirement
                        )
                    except CommandRuleError as exc:
                        signal(
                            kind="invalid_semantic_proposal",
                            category="unsafe",
                            summary="A semantic proposal failed canonical revalidation.",
                            detail=str(exc),
                        )
                    else:
                        missing = [
                            change
                            for change in required
                            if not self._available_governed_change_grants(
                                generation_id=requirement.generation_id,
                                task_id=requirement.task_id,
                                operation_id=requirement.operation_id,
                                required=[change],
                            )
                        ]
                        if not missing:
                            signal(
                                kind="authorized_semantic_proposal",
                                category="system",
                                summary="The approved proposal is ready for mechanical application.",
                            )
                        else:
                            signal(
                                kind="proposal_review",
                                category="needs_marco",
                                summary="An exact governed change is waiting for Marco's approval.",
                                detail=str(payload.get("reason") or ""),
                                action={
                                    "kind": "semantic_proposal",
                                    "proposal_id": str(requirement.requirement_id),
                                    "dish_id": str(operation.task_id),
                                    "operation_id": str(operation.operation_id),
                                    "cycle_id": str(requirement.cycle_id),
                                    "candidate_identity": payload["candidate"]["identity"],
                                    "required_authorizations": missing,
                                    "linked_changes": list(payload.get("linked_changes") or ()),
                                },
                            )
                else:
                    signal(
                        kind="human_decision",
                        category="needs_marco",
                        summary="Dish is waiting for a Marco decision.",
                        detail=requirement.question,
                        action={
                            "kind": "record_human_decision",
                            "dish_id": str(operation.task_id),
                            "operation_id": str(operation.operation_id),
                            "requirement_id": str(requirement.requirement_id),
                            "cycle_id": str(requirement.cycle_id) if requirement.cycle_id else None,
                            "hold_identity": version.content_identity if version is not None else None,
                            "resume_status": (
                                "pending-research"
                                if requirement.cycle_id is None
                                else "pending-verification"
                            ),
                        },
                    )
            elif (
                operation.phase == "held_human"
                and latest_cycle is not None
                and latest_cycle.outcome == "verification-hold"
            ):
                signal(
                    kind="verification_hold",
                    category="needs_marco",
                    summary="Repeated Verification stopped for Marco's decision.",
                    action={
                        "kind": "resolve_verification_hold",
                        "dish_id": str(operation.task_id),
                        "operation_id": str(operation.operation_id),
                        "cycle_id": str(latest_cycle.cycle_id),
                        "hold_identity": version.content_identity if version is not None else None,
                    },
                )

            lease = self.session.scalar(
                select(wf.ServiceLease)
                .where(
                    wf.ServiceLease.operation_id == operation.operation_id,
                    wf.ServiceLease.lease_kind == "actor",
                    wf.ServiceLease.state == "active",
                    wf.ServiceLease.import_run_id.is_(None),
                )
                .order_by(wf.ServiceLease.issued_at.desc())
                .limit(1)
            )
            if lease is not None:
                revoked = self.session.scalar(
                    select(wf.OperationRunRevocation.revocation_id).where(
                        wf.OperationRunRevocation.generation_id == generation.generation_id,
                        wf.OperationRunRevocation.operation_id == operation.operation_id,
                        wf.OperationRunRevocation.owner_id == lease.owner_id,
                        wf.OperationRunRevocation.run_id == lease.run_id,
                        wf.OperationRunRevocation.import_run_id.is_(None),
                    )
                )
                if revoked is not None:
                    signal(
                        kind="revoked_live_lease",
                        category="unsafe",
                        summary="A revoked run is still presented as live ownership.",
                    )
                elif lease.expires_at <= now:
                    signal(
                        kind="expired_lease",
                        category="system",
                        summary="The actor lease expired and is system-recoverable.",
                    )

            if not signals:
                continue
            precedence = {"system": 0, "needs_marco": 1, "unsafe": 2}
            category = max(
                (str(row["category"]) for row in signals), key=precedence.get
            )
            kinds = {str(row["kind"]) for row in signals}
            if category == "unsafe":
                group = "recovery"
            elif "human_decision" in kinds or "verification_hold" in kinds:
                group = "human_review"
            elif "evidence_hold" in kinds:
                group = "evidence"
            elif "proposal_review" in kinds:
                group = "change_review"
            else:
                group = "system"
            item = {
                "dish_id": str(operation.task_id),
                "task_id": str(operation.task_id),
                "task_gid": str(task_gid) if task_gid is not None else None,
                "task_title": version.title if version is not None else None,
                "operation_id": str(operation.operation_id),
                "category": category,
                "needs_you": category in {"needs_marco", "unsafe"},
                "queue_group": group,
                "signals": signals,
            }
            category_counts[category] += 1
            items.append(item)

        items.sort(
            key=lambda item: (
                group_order[str(item["queue_group"])],
                str(item.get("task_title") or item["dish_id"]).casefold(),
                str(item["operation_id"]),
            )
        )
        needs_you_count = sum(bool(item["needs_you"]) for item in items)
        system_count = sum(item["queue_group"] == "system" for item in items)
        active_task_count = len({operation.task_id for operation in operations})
        return {
            "checked_count": len(operations),
            "active_dish_count": active_task_count,
            "live_inspection_count": 0,
            "issue_count": len(items),
            "attention_count": len(items),
            "needs_you_count": needs_you_count,
            "system_count": system_count,
            "healthy_count": max(active_task_count - len(items), 0),
            "category_counts": category_counts,
            "issue_items": items,
            "attention_items": items,
            "read_only": True,
            "source": "postgresql_authority",
            "message": "Queue built exclusively from canonical PostgreSQL workflow state.",
        }

    def _holds(self) -> Mapping[str, Any]:
        generation = self.reads.active_generation()
        operations = self.session.scalars(
            select(wf.WorkflowOperation)
            .where(
                wf.WorkflowOperation.generation_id == generation.generation_id,
                wf.WorkflowOperation.lifecycle == "open",
                wf.WorkflowOperation.phase.in_(("held_evidence", "held_human")),
            )
            .order_by(wf.WorkflowOperation.created_at, wf.WorkflowOperation.operation_id)
        ).all()
        rows: list[dict[str, Any]] = []
        for operation in operations:
            task = self.session.get(models.DishTask, operation.task_id)
            state = self.session.get(
                models.DishState,
                (generation.generation_id, operation.task_id),
            )
            version = (
                self.session.get(models.ContentVersion, state.current_content_version_id)
                if state is not None
                else None
            )
            cycle = self.session.scalar(
                select(wf.VerificationCycle)
                .where(wf.VerificationCycle.operation_id == operation.operation_id)
                .order_by(wf.VerificationCycle.cycle_sequence.desc())
                .limit(1)
            )
            hold = self.session.scalar(
                select(wf.EvidenceHold)
                .where(
                    wf.EvidenceHold.operation_id == operation.operation_id,
                    wf.EvidenceHold.state == "open",
                )
                .order_by(wf.EvidenceHold.opened_at.desc())
                .limit(1)
            )
            requirement = self.session.scalar(
                select(wf.HumanReviewRequirement)
                .where(wf.HumanReviewRequirement.operation_id == operation.operation_id)
                .order_by(wf.HumanReviewRequirement.opened_at.desc())
                .limit(1)
            )
            if cycle is not None and cycle.outcome == "verification-hold":
                hold_class = "verification_two_pass"
                required_action = "resolved"
            elif operation.phase == "held_evidence":
                hold_class = (
                    "research_preconstruction_evidence"
                    if hold is not None and hold.cycle_id is None
                    else "verification_evidence"
                )
                required_action = "supply-evidence"
            else:
                hold_class = (
                    "research_preconstruction_human"
                    if requirement is not None and requirement.cycle_id is None
                    else "verification_human"
                )
                required_action = (
                    "reopen"
                    if requirement is not None and requirement.state == "decided"
                    else "record-human-decision"
                )
            status_detail = None
            if version is not None:
                try:
                    parts = parse_canonical_document(
                        title=version.title, body=version.body
                    )
                    status_detail = parts.document.state.values.get("Status detail")
                except CanonicalDocumentError:
                    status_detail = None
            rows.append(
                {
                    "hold_class": hold_class,
                    "required_admin_action": required_action,
                    "task_id": str(operation.task_id),
                    "task_title": version.title if version is not None else None,
                    "operation_id": str(operation.operation_id),
                    "cycle_id": str(cycle.cycle_id) if cycle is not None else None,
                    "hold_id": str(hold.hold_id) if hold is not None else None,
                    "requirement_id": (
                        str(requirement.requirement_id)
                        if requirement is not None
                        else None
                    ),
                    "hold_identity": (
                        version.content_identity if version is not None else None
                    ),
                    "question": status_detail,
                    "phase": operation.phase,
                    "created_at": operation.created_at.isoformat(),
                    "task_exists": task is not None,
                }
            )
        return {"holds": rows, "count": len(rows)}

    def _binding_for(self, generation: models.AuthorityGeneration) -> models.HonestContractBinding:
        try:
            contract = RegistryRepository(self.session).active_release_contract(
                generation.generation_id
            )
        except CoreAuthorityError as exc:
            raise CommandRuleError(
                "CONTRACT_BINDING_MISSING", str(exc)
            ) from exc
        if contract.generation.generation_id != generation.generation_id:
            raise CommandRuleError(
                "CONTRACT_BINDING_MISSING",
                "active registry contract does not belong to runtime generation",
            )
        return contract.honest_binding

    def _resolve_targets(
        self, call: CommandCall
    ) -> tuple[models.DishTask | None, wf.WorkflowOperation | None]:
        definition = definition_for(call.command_name)
        verification_start = (
            call.command_name == "start"
            and str(call.arguments.get("kind", "")) == "verification"
        )
        prepared_start = (
            call.command_name == "start"
            and call.arguments.get("prepared_operation_id") is not None
        )
        operation = None
        task = None
        if call.command_name in {"apply-proposal", "review-reject"}:
            requirement = self._semantic_proposal_requirement(
                str(call.arguments.get("proposal_id") or "")
            )
            operation = self.session.get(
                wf.WorkflowOperation, requirement.operation_id
            )
            task = self.session.get(models.DishTask, requirement.task_id)
            if operation is None or task is None:
                raise CommandRuleError(
                    "SEMANTIC_PROPOSAL_STALE",
                    "proposal workflow authority is incomplete",
                )
        elif prepared_start:
            try:
                prepared_id = uuid.UUID(str(call.arguments["prepared_operation_id"]))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_OPERATION_ID",
                    "prepared operation identifier must be a UUID",
                    http_status=400,
                ) from exc
            operation = self.session.get(wf.WorkflowOperation, prepared_id)
            if operation is None:
                raise CommandRuleError(
                    "PREPARED_OPERATION_NOT_FOUND",
                    "prepared successor operation is missing",
                    http_status=404,
                )
            task = self.session.get(models.DishTask, operation.task_id)
        operation_ref = call.arguments.get("operation_id") or call.arguments.get("submission_id")
        if verification_start and operation_ref is not None:
            raise CommandRuleError(
                "ARGUMENT_UNEXPECTED",
                "Verification start accepts only the exact target_operation_id/target_cycle_id pair returned by Dish, or neither for an ordinary start",
                http_status=400,
                data={"field": "operation_id" if "operation_id" in call.arguments else "submission_id"},
            )
        if operation_ref:
            try:
                operation = self.session.get(wf.WorkflowOperation, uuid.UUID(str(operation_ref)))
            except ValueError as exc:
                raise CommandRuleError("INVALID_OPERATION_ID", "operation identifier must be a UUID", http_status=400) from exc
            if operation is None:
                raise CommandRuleError("OPERATION_NOT_FOUND", "unknown workflow operation", http_status=404)
        task_ref = (
            call.arguments.get("dish_id")
            or call.arguments.get("task_id")
            or call.arguments.get("task_gid")
        )
        if not task_ref:
            dish_ref = call.arguments.get("dish")
            if dish_ref:
                task_ref = _task_reference_from_dish(str(dish_ref))
        if task_ref:
            try:
                task = self.reads.resolve_task(str(task_ref))
            except ReadModelError as exc:
                raise CommandRuleError("TASK_NOT_FOUND", str(exc), http_status=404) from exc
        elif operation is not None and task is None:
            task = self.session.get(models.DishTask, operation.task_id)
        if definition.task_required and task is None:
            raise CommandRuleError("TASK_REQUIRED", "command requires a task", http_status=400)
        if verification_start:
            target_operation = call.arguments.get("target_operation_id")
            target_cycle = call.arguments.get("target_cycle_id")
            if (target_operation is None) != (target_cycle is None):
                missing = (
                    "target_cycle_id"
                    if target_operation is not None
                    else "target_operation_id"
                )
                raise CommandRuleError(
                    "VERIFICATION_TARGET_PAIR_REQUIRED",
                    "Verification targets are only for an exact Dish-returned abandonment continuation; supply both returned values together, or omit both for an ordinary Verification start",
                    http_status=400,
                    data={"field": missing},
                )
            if target_operation is not None:
                try:
                    operation = self.session.get(
                        wf.WorkflowOperation, uuid.UUID(str(target_operation))
                    )
                except ValueError as exc:
                    raise CommandRuleError(
                        "INVALID_OPERATION_ID",
                        "target operation identifier must be a UUID",
                        http_status=400,
                    ) from exc
                try:
                    uuid.UUID(str(target_cycle))
                except ValueError as exc:
                    raise CommandRuleError(
                        "INVALID_CYCLE_ID",
                        "target cycle identifier must be a UUID",
                        http_status=400,
                    ) from exc
                if operation is None or operation.task_id != task.task_id:
                    raise CommandRuleError(
                        "VERIFICATION_TARGET_STALE",
                        "Verification start target no longer belongs to this task",
                    )
            else:
                active_generation_id = self.reads.active_generation().generation_id
                operations = list(
                    self.session.scalars(
                        select(wf.WorkflowOperation)
                        .where(
                            wf.WorkflowOperation.generation_id
                            == active_generation_id,
                            wf.WorkflowOperation.task_id == task.task_id,
                            wf.WorkflowOperation.lifecycle == "open",
                        )
                        .order_by(
                            wf.WorkflowOperation.created_at.desc(),
                            wf.WorkflowOperation.operation_id.desc(),
                        )
                        .limit(2)
                    )
                )
                if not operations:
                    raise CommandRuleError(
                        "OPEN_OPERATION_REQUIRED",
                        "task has no open Verification operation",
                    )
                if len(operations) != 1:
                    raise CommandRuleError(
                        "VERIFICATION_OPERATION_AMBIGUOUS",
                        "task does not have one unique open Verification operation",
                    )
                operation = operations[0]
        if definition.operation_required and operation is None:
            raise CommandRuleError(
                "OPERATION_ID_REQUIRED",
                "command requires the exact operation identifier",
                http_status=400,
            )
        if task is not None and operation is not None and operation.task_id != task.task_id:
            raise CommandRuleError("TARGET_MISMATCH", "task and operation do not match")
        return task, operation

    def _planner_snapshot(
        self,
        generation_id: uuid.UUID,
        task: models.DishTask | None,
        operation: wf.WorkflowOperation | None,
    ) -> AuthoritativeSnapshot:
        if task is None:
            return AuthoritativeSnapshot(generation_id=str(generation_id), task_id=None, fence=None, workflow=None, task_exists=False)
        view = self.reads.task_view(task.task_id)
        workflow_snapshot = None
        hold_reject_cycle_exists = False
        hold_reject_evidence_hold_exists = False
        hold_reject_human_review_exists = False
        hold_reject_baseline_matches = False
        hold_reject_candidate_activation_exists = False
        hold_reject_author_owner_id = None
        hold_reject_author_run_id = None
        hold_reject_author_lease_id = None
        hold_reject_author_lease_expires_at = None
        hold_reject_registered_agent_matches = False
        if operation is not None:
            workflow_snapshot = self.reads._workflow_snapshot(
                generation_id=generation_id,
                task_id=task.task_id,
                title=view.title,
                body=view.body,
                operation=operation,
            )
            proposal_status, proposal_actionable = self._semantic_proposal_status(
                operation
            )
            if proposal_status is not None:
                workflow_snapshot = replace(
                    workflow_snapshot,
                    semantic_proposal_status=proposal_status,
                    semantic_proposal_actionable=proposal_actionable,
                )
            hold_reject_cycle_exists = self.session.scalar(
                select(wf.VerificationCycle.cycle_id)
                .where(wf.VerificationCycle.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            hold_reject_evidence_hold_exists = self.session.scalar(
                select(wf.EvidenceHold.hold_id)
                .where(wf.EvidenceHold.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            hold_reject_human_review_exists = self.session.scalar(
                select(wf.HumanReviewRequirement.requirement_id)
                .where(wf.HumanReviewRequirement.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            creation_fence = self.session.get(
                wf.TaskExecutionFence, operation.creation_execution_id
            )
            if creation_fence is not None:
                hold_reject_baseline_matches = bool(
                    view.dish_version == creation_fence.expected_dish_version
                    and view.membership_revision
                    == creation_fence.expected_membership_revision
                )
            hold_reject_candidate_activation_exists = self.session.scalar(
                select(models.DishMutationReceipt.dish_version)
                .join(
                    wf.CommandExecution,
                    wf.CommandExecution.execution_id
                    == models.DishMutationReceipt.command_execution_id,
                )
                .where(
                    wf.CommandExecution.operation_id == operation.operation_id,
                    models.DishMutationReceipt.content_changed.is_(True),
                )
                .limit(1)
            ) is not None
            authors = list(
                self.session.scalars(
                    select(wf.OperationActorFact)
                    .where(
                        wf.OperationActorFact.operation_id == operation.operation_id,
                        wf.OperationActorFact.actor_role == "author",
                    )
                    .order_by(wf.OperationActorFact.recorded_at, wf.OperationActorFact.actor_fact_id)
                    .limit(2)
                )
            )
            if len(authors) == 1:
                author = authors[0]
                hold_reject_author_owner_id = author.owner_id
                hold_reject_author_run_id = str(author.run_id)
                run = self.session.get(wf.ServiceRun, author.run_id)
                hold_reject_registered_agent_matches = bool(
                    run is not None
                    and run.generation_id == generation_id
                    and run.status == "active"
                    and run.owner_id == author.owner_id
                    and run.agent == author.agent
                )
                leases = list(
                    self.session.scalars(
                        select(wf.ServiceLease)
                        .where(
                            wf.ServiceLease.operation_id == operation.operation_id,
                            wf.ServiceLease.task_id == task.task_id,
                            wf.ServiceLease.lease_kind == "actor",
                            wf.ServiceLease.actor_role == "author",
                            wf.ServiceLease.actor_attempt_sequence
                            == author.actor_attempt_sequence,
                            wf.ServiceLease.owner_id == author.owner_id,
                            wf.ServiceLease.run_id == author.run_id,
                            wf.ServiceLease.state == "active",
                        )
                        .order_by(wf.ServiceLease.issued_at.desc())
                        .limit(2)
                    )
                )
                if len(leases) == 1:
                    hold_reject_author_lease_id = str(leases[0].lease_id)
                    hold_reject_author_lease_expires_at = leases[0].expires_at
        return AuthoritativeSnapshot(
            generation_id=str(generation_id),
            task_id=str(task.task_id),
            fence=AuthorityFence(
                dish_version=view.dish_version,
                membership_revision=view.membership_revision,
                operation_revision=operation.operation_revision if operation else None,
                operation_phase=operation.phase if operation else None,
            ),
            workflow=workflow_snapshot,
            task_exists=True,
            current_content_version_id=str(view.content_version_id),
            current_section_id=str(view.section_id),
            completed=view.completed,
            active_lease_id=self._active_lease_id(task.task_id),
            unresolved_projection_attempt_id=self._unresolved_projection_attempt_id(task.task_id),
            open_hold_id=self._open_id(wf.EvidenceHold, task.task_id),
            open_human_requirement_id=self._open_id(wf.HumanReviewRequirement, task.task_id),
            open_abandonment_id=self._open_abandonment_id(generation_id, task.task_id),
            open_operation_id=str(view.operation_id) if view.operation_id else None,
            hold_reject_cycle_exists=hold_reject_cycle_exists,
            hold_reject_evidence_hold_exists=hold_reject_evidence_hold_exists,
            hold_reject_human_review_exists=hold_reject_human_review_exists,
            hold_reject_baseline_matches=hold_reject_baseline_matches,
            hold_reject_candidate_activation_exists=hold_reject_candidate_activation_exists,
            hold_reject_author_owner_id=hold_reject_author_owner_id,
            hold_reject_author_run_id=hold_reject_author_run_id,
            hold_reject_author_lease_id=hold_reject_author_lease_id,
            hold_reject_author_lease_expires_at=hold_reject_author_lease_expires_at,
            hold_reject_registered_agent_matches=hold_reject_registered_agent_matches,
        )

    def _unresolved_projection_attempt_id(self, task_id: uuid.UUID) -> str | None:
        value = self.projection_recorder.unresolved_attempt_id(task_id)
        return str(value) if value else None

    def _active_lease_id(self, task_id: uuid.UUID) -> str | None:
        value = self.session.scalar(select(wf.ServiceLease.lease_id).where(wf.ServiceLease.task_id == task_id, wf.ServiceLease.state == "active"))
        return str(value) if value else None

    def _open_id(self, model: Any, task_id: uuid.UUID) -> str | None:
        state = "open"
        identity = model.hold_id if model is wf.EvidenceHold else model.requirement_id
        value = self.session.scalar(select(identity).where(model.task_id == task_id, model.state == state))
        return str(value) if value else None

    def _open_abandonment_id(
        self, generation_id: uuid.UUID, task_id: uuid.UUID
    ) -> str | None:
        value = self.session.scalar(
            select(wf.AbandonmentAttempt.abandonment_id).where(
                wf.AbandonmentAttempt.generation_id == generation_id,
                wf.AbandonmentAttempt.task_id == task_id,
                wf.AbandonmentAttempt.state.in_(("preparing", "published", "blocked", "reconciling")),
            )
        )
        return str(value) if value else None

    def _validate_planning_intent_basis(
        self, call: CommandCall, *, initial: bool
    ) -> None:
        basis = call.arguments.get("intent_basis")
        reason = call.arguments.get("override_reason")
        if initial:
            if basis is not None or reason is not None:
                raise CommandRuleError(
                    "PLANNING_CONFIRMATION_NOT_YET_ISSUED",
                    "the first Planning request cannot supply intent confirmation fields",
                    http_status=400,
                )
            return
        if basis not in {"user_requested", "agent_override"}:
            raise CommandRuleError(
                "PLANNING_INTENT_BASIS_REQUIRED",
                "Planning confirmation requires user_requested or agent_override",
                http_status=400,
            )
        if basis == "agent_override" and not str(reason or "").strip():
            raise CommandRuleError(
                "PLANNING_OVERRIDE_REASON_REQUIRED",
                "agent_override requires a non-blank override_reason",
                http_status=400,
            )
        if basis == "user_requested" and reason is not None:
            raise CommandRuleError(
                "PLANNING_OVERRIDE_REASON_NOT_ALLOWED",
                "override_reason is not allowed with user_requested",
                http_status=400,
            )

    def _validate_planning_agent(
        self, *, generation_id: uuid.UUID, call: CommandCall
    ) -> str:
        agent = str(call.arguments.get("agent", "")).strip()
        if not agent:
            raise CommandRuleError(
                "AGENT_REQUIRED",
                "Planning start requires the exact registered agent",
                http_status=400,
            )
        run = self.workflow.repo.require_active_run(
            generation_id=generation_id, run_id=call.run_id, owner_id=call.owner_id
        )
        if run.agent != agent:
            raise CommandRuleError(
                "PLANNING_AGENT_MISMATCH",
                "Planning start agent does not match the registered service run",
            )
        return agent

    def _assert_planning_challenge_target(
        self, *, challenge: wf.PlanningIntentChallenge, call: CommandCall
    ) -> None:
        issuing = self.session.get(wf.ServiceRequest, challenge.issuing_request_id)
        if issuing is None:
            raise CommandRuleError(
                "PLANNING_CHALLENGE_EVIDENCE_MISSING",
                "the challenge issuing request is missing",
            )
        issued_arguments = dict(issuing.canonical_payload.get("arguments") or {})
        confirming_arguments = dict(call.arguments)
        for key in ("intent_challenge_id", "intent_basis", "override_reason"):
            confirming_arguments.pop(key, None)
        if issued_arguments != confirming_arguments:
            raise CommandRuleError(
                "PLANNING_CHALLENGE_MISMATCH",
                "Planning confirmation does not match the exact issued target",
                data={
                    "issuing_request_id": str(challenge.issuing_request_id),
                    "challenge_id": str(challenge.challenge_id),
                },
            )
