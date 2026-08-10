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
        cards_by_id = {card.task_id: card for card in facts.cards}
        latest_by_task: dict[object, datetime] = {}
        for event in facts.events:
            latest_by_task.setdefault(event.task_id, event.occurred_at)

        dishes = []
        needs_you_count = 0
        system_count = 0
        human_review_count = 0
        recovery_count = 0
        for card in facts.cards:
            codes = self._attention_codes(card)
            if not codes:
                continue
            needs_you = any(code in NEEDS_YOU_CODES for code in codes)
            if needs_you:
                needs_you_count += 1
            else:
                system_count += 1
            if "verification_attention" in codes:
                human_review_count += 1
            if "lease_attention" in codes or "recovery_required" in codes:
                recovery_count += 1
            dishes.append({
                "task_id": str(card.task_id),
                "title": card.title.strip(),
                "section_label": section_labels.get(card.section_id, "Unknown section"),
                "workflow_status": operation_status(card.operation_kind, card.operation_phase),
                "bucket": "needs_you" if needs_you else "system_activity",
                "attention": [self._attention_item(code) for code in codes],
                "last_activity_at": self._iso(latest_by_task.get(card.task_id)),
                "diagnostics": {"attention_codes": codes},
            })

        dishes.sort(key=lambda item: (item["bucket"] != "needs_you", item["title"].casefold(), item["task_id"]))
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
    def _attention_item(code: str) -> dict[str, str]:
        label, message = _ADMIN_ATTENTION[code]
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
