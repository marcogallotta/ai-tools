"""Read-only operator presentation for the frontend admin prototype."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dish_pg.frontend_admin_query import FrontendAdminFacts, FrontendAdminQuery
from dish_service.frontend_contract import ATTENTION_BY_CODE, operation_status


NEEDS_YOU_CODES = frozenset({"lease_attention", "verification_attention", "recovery_required"})
SYSTEM_CODES = frozenset({"isolated", "hold_active", "abandonment_active", "succession_active", "projection_abnormal"})
_ADMIN_ATTENTION = {
    "isolated": ("Dish is isolated", "This dish is outside the ordinary workflow path."),
    "lease_attention": ("Run authority expired", "Choose whether this invocation should be replaced before work continues."),
    "verification_attention": ("Waiting for your decision", "Human Review is open for this dish."),
    "hold_active": ("Workflow is deliberately paused", "A recorded hold is active; no immediate operator action is inferred."),
    "recovery_required": ("Recovery needs your attention", "An uncertain execution must be settled before ordinary progress can continue."),
    "abandonment_active": ("Replacement is being prepared", "Dish is processing an abandonment/replacement sequence."),
    "succession_active": ("Replacement continuation exists", "A successor operation is active for this dish."),
    "projection_abnormal": ("External projection needs attention", "The downstream projection is delayed, blocked, uncertain, or drifting."),
    "research_required": ("Needs research", "This dish is waiting in the Research queue."),
    "verification_required": ("Needs verification", "This dish is waiting in the Verification queue."),
}


@dataclass(frozen=True, slots=True)
class FrontendAdminConfig:
    projection_delay: timedelta
    max_cards: int = 5000
    max_events: int = 120

    def __post_init__(self) -> None:
        if self.projection_delay <= timedelta(0):
            raise ValueError("projection delay must be positive")
        if not 1 <= self.max_cards <= 20000:
            raise ValueError("max_cards must be between 1 and 20000")
        if not 1 <= self.max_events <= 500:
            raise ValueError("max_events must be between 1 and 500")


class FrontendAdminService:
    def __init__(self, query: FrontendAdminQuery, *, environment: str, config: FrontendAdminConfig) -> None:
        self.query = query
        self.environment = environment
        self.config = config

    def read(self) -> dict[str, Any]:
        return self.present(self.query.capture(
            projection_delay=self.config.projection_delay,
            max_cards=self.config.max_cards,
            max_events=self.config.max_events,
        ))

    def present(self, facts: FrontendAdminFacts) -> dict[str, Any]:
        section_labels = {section.section_id: section.section_label for section in facts.sections}
        section_roles = {section.section_id: section.workflow_role for section in facts.sections}
        cards_by_id = {card.task_id: card for card in facts.cards}
        current_human_review_by_task = {}
        for human_review in facts.human_reviews:
            current_human_review_by_task.setdefault(human_review.task_id, human_review)
        latest_by_task: dict[object, datetime] = {}
        for event in facts.events:
            latest_by_task.setdefault(event.task_id, event.occurred_at)

        dishes = []
        needs_you_count = 0
        system_count = 0
        workflow_queue_count = 0
        human_review_count = 0
        recovery_count = 0
        research_count = 0
        verification_count = 0
        for card in facts.cards:
            codes = self._attention_codes(card)
            has_active_operation = card.operation_kind is not None or card.operation_phase is not None
            workflow_code = self._workflow_queue_code(card, section_roles.get(card.section_id))
            if workflow_code is not None:
                codes.append(workflow_code)
            if not codes and not has_active_operation:
                continue
            needs_you = any(code in NEEDS_YOU_CODES for code in codes)
            has_system_activity = has_active_operation or any(code in SYSTEM_CODES for code in codes)
            if needs_you:
                bucket = "needs_you"
                needs_you_count += 1
            elif has_system_activity:
                bucket = "system_activity"
                system_count += 1
            else:
                bucket = "workflow_queue"
                workflow_queue_count += 1
            if "verification_attention" in codes:
                human_review_count += 1
            if "lease_attention" in codes or "recovery_required" in codes:
                recovery_count += 1
            if workflow_code == "research_required":
                research_count += 1
            elif workflow_code == "verification_required":
                verification_count += 1
            dishes.append({
                "task_id": str(card.task_id),
                "title": card.title.strip(),
                "section_label": section_labels.get(card.section_id, "Unknown section"),
                "workflow_status": operation_status(card.operation_kind, card.operation_phase),
                "bucket": bucket,
                "attention": [
                    self._attention_item(code, current_human_review_by_task.get(card.task_id))
                    for code in codes
                ],
                "last_activity_at": self._iso(latest_by_task.get(card.task_id)),
                "diagnostics": {"attention_codes": codes},
            })

        bucket_order = {"needs_you": 0, "workflow_queue": 1, "system_activity": 2}
        dishes.sort(key=lambda item: (bucket_order[item["bucket"]], item["title"].casefold(), item["task_id"]))
        journal = []
        for event in facts.events:
            card = cards_by_id.get(event.task_id)
            if card is None:
                continue
            journal.append({
                "event_id": str(event.audit_event_id),
                "task_id": str(event.task_id),
                "title": card.title.strip(),
                "occurred_at": self._iso(event.occurred_at),
                "summary": self._event_summary(event.event_type),
                "diagnostics": {
                    "event_type": event.event_type,
                    "actor": event.actor,
                    "request_id": str(event.request_id),
                    "command_execution_id": self._route_or_none(event.command_execution_id),
                    "operation_id": self._route_or_none(event.operation_id),
                },
            })
        return {
            "generated_at": self._iso(facts.evaluation_time),
            "summary": {
                "needs_you": needs_you_count,
                "human_review": human_review_count,
                "recovery": recovery_count,
                "workflow_queue": workflow_queue_count,
                "research": research_count,
                "verification": verification_count,
                "system_activity": system_count,
                "affected_dishes": len(dishes),
            },
            "dishes": dishes,
            "journal": journal,
        }

    @staticmethod
    def _attention_codes(card) -> list[str]:
        active = {
            "isolated": card.isolated,
            "lease_attention": card.lease_attention,
            "verification_attention": card.verification_attention,
            "hold_active": card.hold_active,
            "recovery_required": card.recovery_required,
            "abandonment_active": card.abandonment_active,
            "succession_active": card.succession_active,
            "projection_abnormal": card.projection_abnormal,
        }
        return [code for code in ATTENTION_BY_CODE if active.get(code, False)]

    @staticmethod
    def _workflow_queue_code(card, section_role: str | None) -> str | None:
        # Queue placement is a factual fallback only when no open operation exists,
        # matching the task-detail advisory contract. An open operation is more
        # specific than broad queue placement.
        if card.operation_kind is not None or card.operation_phase is not None:
            return None
        if section_role == "research_queue":
            return "research_required"
        if section_role == "verification_queue":
            return "verification_required"
        return None

    @staticmethod
    def _attention_item(code: str, human_review=None) -> dict[str, str]:
        label, message = _ADMIN_ATTENTION[code]
        if code == "verification_attention" and human_review is not None:
            message = human_review.question
        return {"code": code, "label": label, "message": message}

    @staticmethod
    def _event_summary(event_type: str) -> str:
        fixed = {
            "planning_intent_challenge_issued": "Planning needs clarification",
            "task_missing": "Task was unavailable",
            "operation_missing": "Operation was unavailable",
            "workflow_action_rejected": "Workflow action was rejected",
            "projection_target_missing": "Projection target was unavailable",
            "abandonment_fence_rejected": "Abandonment fence was rejected",
            "retired_command_rejected": "Retired command was rejected",
            "principal_scope_rejected": "Command scope was rejected",
            "section2_recovery_boundary": "Recovery checkpoint recorded",
        }
        if event_type in fixed:
            return fixed[event_type]
        suffixes = (
            ("_validation_rejected", " validation was rejected"),
            ("_committed", " completed"),
            ("_rejected", " was rejected"),
            ("_planned", " prepared"),
        )
        for suffix, ending in suffixes:
            if event_type.endswith(suffix):
                command = event_type[: -len(suffix)].replace("_", " ").strip()
                return f"{command.capitalize()}{ending}"
        return event_type.replace("_", " ").strip().capitalize()

    @staticmethod
    def _iso(value) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _route_or_none(value) -> str | None:
        return str(value) if value is not None else None
