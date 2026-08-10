from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

SECTION_RESEARCH = "r1s-" + "r" * 27
SECTION_VERIFY = "r1s-" + "v" * 27
SECTION_READY = "r1s-" + "c" * 27

TASK_ALPHA = "11111111-1111-4111-8111-111111111111"
TASK_BETA = "22222222-2222-4222-8222-222222222222"
TASK_GAMMA = "33333333-3333-4333-8333-333333333333"
TASK_DELTA = "44444444-4444-4444-8444-444444444444"
TASK_EPSILON = "55555555-5555-4555-8555-555555555555"
TASK_ZETA = "66666666-6666-4666-8666-666666666666"
TASK_ETA = "77777777-7777-4777-8777-777777777777"
TASK_THETA = "88888888-8888-4888-8888-888888888888"
TASK_IOTA = "99999999-9999-4999-8999-999999999999"
TASK_KAPPA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

ATTENTION_ORDER = (
    "isolated",
    "lease_attention",
    "verification_attention",
    "hold_active",
    "recovery_required",
    "abandonment_active",
    "succession_active",
    "projection_abnormal",
)
ATTENTION_SEVERITY = {
    "isolated": "warning",
    "lease_attention": "warning",
    "verification_attention": "warning",
    "hold_active": "warning",
    "recovery_required": "error",
    "abandonment_active": "error",
    "succession_active": "error",
    "projection_abnormal": "warning",
}
DISCLOSURES = {
    "lease_attention": ("lease", "Lease", "The current run authority requires operator attention."),
    "verification_attention": ("verification", "Verification", "A Human Review decision is pending."),
    "hold_active": ("hold", "Hold", "The current workflow has an active hold."),
    "recovery_required": ("recovery", "Recovery", "Recovery is required before normal work continues."),
    "abandonment_active": ("abandonment", "Abandonment", "An abandonment workflow is active."),
    "succession_active": ("succession", "Succession", "A successor workflow is active."),
}
DISCLOSURE_ORDER = ("lease", "verification", "hold", "recovery", "abandonment", "succession")


@dataclass(slots=True)
class CardSpec:
    task_id: str
    title: str
    section_id: str = SECTION_RESEARCH
    attention: tuple[str, ...] = ()
    operation: str | None = None
    phase: str | None = None
    body_html: str = "<p>Canonical content.</p>"
    fallback_text: str | None = None
    advisory_code: str = "workflow.none"
    advisory_message: str = "No next step is currently available."
    projection_state: str | None = None
    projection_message: str = "The downstream projection needs attention."

    def workflow_status(self) -> dict[str, str]:
        if self.operation is None:
            return {"state": "no_active_operation"}
        return {"state": "active_operation", "operation": self.operation, "phase": self.phase or "Prepare required"}


@dataclass(slots=True)
class BoardState:
    sections: list[tuple[str, str]] = field(default_factory=lambda: [
        (SECTION_RESEARCH, "Research Queue"),
        (SECTION_VERIFY, "Verification Queue"),
        (SECTION_READY, "Ready to cook"),
    ])
    cards: list[CardSpec] = field(default_factory=list)
    continuations: dict[str, list[CardSpec]] = field(default_factory=dict)
    snapshot_generation: int = 1

    def snapshot_id(self) -> str:
        return f"stage7-snapshot-{self.snapshot_generation}"

    def bump(self) -> None:
        self.snapshot_generation += 1

    def card(self, task_id: str) -> CardSpec | None:
        return next((item for item in self.cards if item.task_id == task_id), None)


def ordered_attention(codes: Iterable[str]) -> list[str]:
    active = set(codes)
    return [code for code in ATTENTION_ORDER if code in active]


def card_dto(card: CardSpec) -> dict:
    return {
        "task_id": card.task_id,
        "title": card.title,
        "section_id": card.section_id,
        "workflow_status": card.workflow_status(),
        "attention_codes": ordered_attention(card.attention),
    }


def notices_for(cards: Iterable[CardSpec]) -> list[dict]:
    return [
        {"code": code, "task_id": card.task_id, "severity": ATTENTION_SEVERITY[code]}
        for card in cards
        for code in ordered_attention(card.attention)
    ]


def board_payload(state: BoardState, *, page_size: int = 3) -> dict:
    sections = []
    visible: list[CardSpec] = []
    for section_id, label in state.sections:
        cards = [card for card in state.cards if card.section_id == section_id]
        first = cards[:page_size]
        visible.extend(first)
        has_more = len(cards) > page_size or bool(state.continuations.get(section_id))
        sections.append({
            "section_id": section_id,
            "section_label": label,
            "continuity_id": f"stage7-{section_id}-continuity",
            "cards": [card_dto(card) for card in first],
            "next_cursor": f"stage7:{section_id}:1" if first and has_more else None,
        })
    return {
        "snapshot_id": state.snapshot_id(),
        "page_size": page_size,
        "sections": sections,
        "notices": notices_for(visible),
    }


def continuation_payload(state: BoardState, section_id: str, cursor: str) -> dict:
    expected = f"stage7:{section_id}:1"
    if cursor != expected:
        raise ValueError("invalid cursor")
    first_page_ids = {
        card.task_id for card in [item for item in state.cards if item.section_id == section_id][:3]
    }
    remaining = [item for item in state.cards if item.section_id == section_id and item.task_id not in first_page_ids]
    remaining.extend(state.continuations.get(section_id, []))
    page = remaining[:100]
    return {
        "section_id": section_id,
        "continuity_id": f"stage7-{section_id}-continuity",
        "cards": [card_dto(card) for card in page],
        "next_cursor": None,
        "notices": notices_for(page),
    }


def detail_payload(card: CardSpec, section_label: str = "Research Queue") -> dict:
    attention = ordered_attention(card.attention)
    disclosures = [DISCLOSURES[code] for code in attention if code in DISCLOSURES]
    disclosures.sort(key=lambda item: DISCLOSURE_ORDER.index(item[0]))
    if card.fallback_text is None:
        body = {"state": "sanitized_html", "html": card.body_html}
    else:
        body = {"state": "plain_text_fallback", "text": card.fallback_text}
    projection = None
    if "projection_abnormal" in attention:
        projection = {
            "state": card.projection_state or "drifted",
            "message": card.projection_message,
            "observation_time": "2026-08-10T18:00:00+00:00",
        }
    notices = [
        {
            "code": code,
            "severity": ATTENTION_SEVERITY[code],
            "message": f"Stage 7 acceptance notice for {code}.",
            "target": {"type": "task", "route_identity": card.task_id},
        }
        for code in attention
    ]
    if card.fallback_text is not None:
        notices.append({
            "code": "render_rejected",
            "severity": "warning",
            "message": "Canonical content is shown as inert plain text.",
            "target": {"type": "task", "route_identity": card.task_id},
        })
    return {
        "task_id": card.task_id,
        "title": card.title,
        "project_label": "Cooking",
        "section_label": section_label,
        "destination_label": None,
        "workflow_status": card.workflow_status(),
        "attention_codes": attention,
        "body_presentation": body,
        "disclosures": [
            {"code": code, "label": label, "detail": detail}
            for code, label, detail in disclosures
        ],
        "advisory": {
            "code": card.advisory_code,
            "message": card.advisory_message,
            "perspective": "workflow",
            "invokable_by_frontend": False,
        },
        "projection": projection,
        "notices": notices,
    }


def empty_admin_payload() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"needs_you": 0, "human_review": 0, "recovery": 0, "workflow_queue": 0, "research": 0, "verification": 0, "system_activity": 0, "affected_dishes": 0},
        "dishes": [],
        "journal": [],
    }
