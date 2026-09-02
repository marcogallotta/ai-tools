"""Read-only Stage 3 board service over PostgreSQL factual read models."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from dish_pg.frontend_board_query import (
    BoardContext,
    BoardRegistryFacts,
    CardFact,
    FrontendBoardQuery,
    SearchFact,
    SectionFact,
)
from dish_service.frontend_contract import (
    ATTENTION_BY_CODE,
    ATTENTION_PRESENTATIONS,
    BOARD_QUERY_CONTRACT_VERSION,
    FRONTEND_CONTRACT_VERSION,
    NORMALIZATION_CONTRACT_VERSION,
    normalize_label,
    operation_status,
)
from dish_service.frontend_tokens import CursorInvalid, CursorStale, opaque_digest, open_cursor, route_identity, seal_cursor

MAX_TITLE_LENGTH = 500
MAX_LABEL_LENGTH = 160
MAX_SEARCH_QUERY_LENGTH = 160
MAX_SEARCH_RESULTS = 50
MAX_ARCHIVED_RESULTS = 5000


class BoardConfigurationInvalid(RuntimeError):
    """Durable board facts cannot be represented by the frontend contract."""


class BoardCapacityExceeded(RuntimeError):
    """A configured Stage 3 response bound would be exceeded."""


@dataclass(frozen=True, slots=True)
class FrontendBoardConfig:
    first_page_size: int = 50
    continuation_page_size: int = 50
    max_sections: int = 100
    cursor_ttl: timedelta = timedelta(hours=24)
    projection_delay: timedelta | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.first_page_size <= 100:
            raise ValueError("first_page_size must be between 1 and 100")
        if not 1 <= self.continuation_page_size <= 100:
            raise ValueError("continuation_page_size must be between 1 and 100")
        if not 1 <= self.max_sections <= 100:
            raise ValueError("max_sections must be between 1 and 100")
        if not timedelta(0) < self.cursor_ttl <= timedelta(days=7):
            raise ValueError("cursor_ttl must be positive and no longer than seven days")
        if self.projection_delay is None or self.projection_delay <= timedelta(0):
            raise ValueError("projection_delay must be explicitly configured")


class FrontendBoardService:
    """Build closed browser DTOs without granting PostgreSQL mutation authority."""

    def __init__(
        self,
        query: FrontendBoardQuery,
        *,
        environment: str,
        token_secret: bytes,
        config: FrontendBoardConfig,
    ) -> None:
        if not environment or len(environment) > 64:
            raise ValueError("frontend environment must be 1..64 characters")
        self.query = query
        self.environment = environment
        self.token_secret = token_secret
        self.config = config

    def bootstrap(self) -> dict[str, Any]:
        registry = self.query.bootstrap_registry()
        sections_config = self._prepare_sections(registry)
        facts = self.query.bootstrap_cards(
            registry=registry,
            page_size=self.config.first_page_size,
            projection_delay=self._projection_delay,
        )
        sections: list[dict[str, Any]] = []
        snapshot_sections: list[dict[str, Any]] = []
        notices: list[dict[str, str]] = []
        for section in sections_config:
            cards = facts.cards_by_section[section.section_id]
            continuity_id = self._continuity_id(facts.context, section.section_id)
            card_dtos = [self._card_dto(card) for card in cards]
            for card, dto in zip(cards, card_dtos, strict=True):
                notices.extend(self._notices(card, dto["task_id"]))
            next_cursor = None
            if facts.has_more_by_section[section.section_id]:
                if not cards:
                    raise BoardConfigurationInvalid("nonterminal section page cannot be empty")
                next_cursor = self._cursor(
                    context=facts.context,
                    section_id=section.section_id,
                    section_ordinal=section.ordinal,
                    continuity_id=continuity_id,
                    boundary=cards[-1],
                )
            section_dto: dict[str, Any] = {
                "section_id": self._section_route(section.section_id),
                "section_label": section.section_label,
                "continuity_id": continuity_id,
                "cards": card_dtos,
                "next_cursor": next_cursor,
            }
            sections.append(section_dto)
            snapshot_section: dict[str, Any] = {
                "section_id": section_dto["section_id"],
                "section_label": section.section_label,
                "continuity_id": continuity_id,
                "cards": card_dtos,
                "has_more": next_cursor is not None,
            }
            snapshot_sections.append(snapshot_section)
        snapshot_id = opaque_digest(
            secret=self.token_secret,
            environment=self.environment,
            purpose="board-snapshot",
            payload={
                "frontend_contract": FRONTEND_CONTRACT_VERSION,
                "board_query_contract": BOARD_QUERY_CONTRACT_VERSION,
                "normalization_contract": NORMALIZATION_CONTRACT_VERSION,
                "page_size": self.config.first_page_size,
                "sections": snapshot_sections,
                "notices": notices,
            },
        )
        return {
            "snapshot_id": snapshot_id,
            "page_size": self.config.first_page_size,
            "sections": sections,
            "notices": notices,
        }

    def continuation(self, *, section_route_id: str, cursor: str) -> dict[str, Any]:
        now = self._utc_now()
        payload = open_cursor(
            cursor,
            secret=self.token_secret,
            environment=self.environment,
            now=now,
        )
        self._validate_cursor_payload(payload, section_route_id=section_route_id)
        try:
            section_id = UUID(payload["section_internal_id"])
            after_task_id = UUID(payload["after_task_internal_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CursorInvalid("cursor identity payload is invalid") from exc
        expected_route = self._section_route(section_id)
        if expected_route != section_route_id:
            raise CursorInvalid("cursor is scoped to another section")
        page = self.query.continuation(
            section_id=section_id,
            after_sort_title=payload["after_sort_title"],
            after_task_id=after_task_id,
            page_size=self.config.continuation_page_size,
            projection_delay=self._projection_delay,
        )
        expected_context = payload["context"]
        if self._context_payload(page.context) != expected_context:
            raise CursorStale("cursor board context is no longer active")
        continuity_id = self._continuity_id(page.context, section_id)
        if continuity_id != payload["continuity_id"]:
            raise CursorStale("cursor continuity contract changed")
        cards = [self._card_dto(card) for card in page.cards]
        notices: list[dict[str, str]] = []
        for card, dto in zip(page.cards, cards, strict=True):
            notices.extend(self._notices(card, dto["task_id"]))
        next_cursor = None
        if page.has_more:
            if not page.cards:
                raise BoardConfigurationInvalid("nonterminal continuation cannot be empty")
            next_cursor = self._cursor(
                context=page.context,
                section_id=section_id,
                section_ordinal=int(payload["section_ordinal"]),
                continuity_id=continuity_id,
                boundary=page.cards[-1],
            )
        return {
            "section_id": section_route_id,
            "continuity_id": continuity_id,
            "cards": cards,
            "next_cursor": next_cursor,
            "notices": notices,
        }

    def search(self, query: str) -> dict[str, Any]:
        normalized = query.strip()
        if not 1 <= len(normalized) <= MAX_SEARCH_QUERY_LENGTH:
            raise ValueError("search query must be 1..160 characters")
        facts = self.query.search_titles(
            query=normalized,
            projection_delay=self._projection_delay,
            max_results=MAX_SEARCH_RESULTS,
        )
        return {
            "results": [self._search_dto(result) for result in facts.results],
            "truncated": facts.truncated,
        }

    def archive(self) -> dict[str, Any]:
        facts = self.query.archived_tasks(max_results=MAX_ARCHIVED_RESULTS)
        return {
            "generated_at": facts.evaluation_time.isoformat(),
            "dishes": [
                {
                    "task_id": self._task_route(item.task_id),
                    "title": item.title.strip(),
                    "archived_at": item.archived_at.isoformat(),
                }
                for item in facts.results
            ],
            "truncated": facts.truncated,
        }

    @property
    def _projection_delay(self) -> timedelta:
        assert self.config.projection_delay is not None
        return self.config.projection_delay

    def _prepare_sections(
        self, facts: BoardRegistryFacts
    ) -> tuple[SectionFact, ...]:
        if len(facts.sections) > self.config.max_sections:
            raise BoardCapacityExceeded("active catalog exceeds configured section capacity")
        normalized_labels: set[str] = set()
        for section in facts.sections:
            if section.section_lifecycle != "active":
                raise BoardConfigurationInvalid("active catalog references a retired section")
            if not 1 <= len(section.section_label) <= MAX_LABEL_LENGTH:
                raise BoardConfigurationInvalid("section label exceeds frontend contract bounds")
            normalized_section = normalize_label(section.section_label)
            if not normalized_section:
                raise BoardConfigurationInvalid("normalized section label must be nonblank")
            if normalized_section in normalized_labels:
                raise BoardConfigurationInvalid("normalized section labels must be unique")
            normalized_labels.add(normalized_section)
        routes: set[str] = set()
        for section in facts.sections:
            route = self._section_route(section.section_id)
            if route in routes:
                raise BoardConfigurationInvalid("section route identity collision")
            routes.add(route)
        return facts.sections

    def _card_dto(self, card: CardFact) -> dict[str, Any]:
        title = card.title.strip()
        if not 1 <= len(title) <= MAX_TITLE_LENGTH:
            raise BoardConfigurationInvalid("task title exceeds frontend contract bounds")
        attention_codes = self._attention_codes(card)
        return {
            "task_id": self._task_route(card.task_id),
            "title": title,
            "section_id": self._section_route(card.section_id),
            "workflow_status": operation_status(card.operation_kind, card.operation_phase),
            "attention_codes": attention_codes,
        }

    def _search_dto(self, result: SearchFact) -> dict[str, str]:
        title = result.title.strip()
        section_label = result.section_label.strip()
        if not 1 <= len(title) <= MAX_TITLE_LENGTH:
            raise BoardConfigurationInvalid("task title exceeds frontend contract bounds")
        if not 1 <= len(section_label) <= MAX_LABEL_LENGTH:
            raise BoardConfigurationInvalid("section label exceeds frontend contract bounds")
        return {
            "task_id": self._task_route(result.task_id),
            "title": title,
            "section_label": section_label,
        }

    @staticmethod
    def _attention_codes(card: CardFact) -> list[str]:
        flags = {
            "isolated": card.isolated,
            "lease_attention": card.lease_attention,
            "verification_attention": card.verification_attention,
            "hold_active": card.hold_active,
            "recovery_required": card.recovery_required,
            "abandonment_active": card.abandonment_active,
            "succession_active": card.succession_active,
            "projection_abnormal": card.projection_abnormal,
        }
        return [item.code for item in ATTENTION_PRESENTATIONS if flags[item.code]]

    def _notices(self, card: CardFact, task_route_id: str) -> list[dict[str, str]]:
        return [
            {
                "code": code,
                "task_id": task_route_id,
                "severity": ATTENTION_BY_CODE[code].severity,
            }
            for code in self._attention_codes(card)
        ]

    def _continuity_id(self, context: BoardContext, section_id: UUID) -> str:
        return opaque_digest(
            secret=self.token_secret,
            environment=self.environment,
            purpose="section-continuity",
            payload={
                **self._context_payload(context),
                "section_id": str(section_id),
                "frontend_contract": FRONTEND_CONTRACT_VERSION,
                "board_query_contract": BOARD_QUERY_CONTRACT_VERSION,
                "normalization_contract": NORMALIZATION_CONTRACT_VERSION,
                "first_page_size": self.config.first_page_size,
                "continuation_page_size": self.config.continuation_page_size,
            },
        )

    def _cursor(
        self,
        *,
        context: BoardContext,
        section_id: UUID,
        section_ordinal: int,
        continuity_id: str,
        boundary: CardFact,
    ) -> str:
        issued_at = self._utc_now()
        return seal_cursor(
            secret=self.token_secret,
            environment=self.environment,
            payload={
                "type": "board-section-continuation",
                "frontend_contract": FRONTEND_CONTRACT_VERSION,
                "board_query_contract": BOARD_QUERY_CONTRACT_VERSION,
                "normalization_contract": NORMALIZATION_CONTRACT_VERSION,
                "section_internal_id": str(section_id),
                "section_ordinal": section_ordinal,
                "continuity_id": continuity_id,
                "page_size": self.config.continuation_page_size,
                "after_sort_title": boundary.sort_title,
                "after_task_internal_id": str(boundary.task_id),
                "context": self._context_payload(context),
                "issued_at": issued_at.isoformat(),
                "expires_at": (issued_at + self.config.cursor_ttl).isoformat(),
            },
        )

    def _validate_cursor_payload(self, payload: dict[str, Any], *, section_route_id: str) -> None:
        if payload.get("type") != "board-section-continuation":
            raise CursorInvalid("cursor has the wrong type")
        expected = {
            "frontend_contract": FRONTEND_CONTRACT_VERSION,
            "board_query_contract": BOARD_QUERY_CONTRACT_VERSION,
            "normalization_contract": NORMALIZATION_CONTRACT_VERSION,
            "page_size": self.config.continuation_page_size,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise CursorStale("cursor contract or page-size configuration changed")
        if not isinstance(payload.get("after_sort_title"), str):
            raise CursorInvalid("cursor ordering boundary is invalid")
        if not isinstance(payload.get("section_ordinal"), int):
            raise CursorInvalid("cursor section ordinal is invalid")
        if not isinstance(payload.get("continuity_id"), str):
            raise CursorInvalid("cursor continuity identity is invalid")
        if not isinstance(payload.get("context"), dict):
            raise CursorInvalid("cursor context is invalid")
        if not isinstance(section_route_id, str):
            raise CursorInvalid("section identity is invalid")

    @staticmethod
    def _context_payload(context: BoardContext) -> dict[str, Any]:
        return {
            "generation_id": str(context.generation_id),
            "catalog_version_id": str(context.catalog_version_id),
            "catalog_revision": context.catalog_revision,
        }

    def _task_route(self, task_id: UUID) -> str:
        return route_identity(
            secret=self.token_secret,
            environment=self.environment,
            kind="task",
            object_id=task_id,
        )

    def _section_route(self, section_id: UUID) -> str:
        return route_identity(
            secret=self.token_secret,
            environment=self.environment,
            kind="section",
            object_id=section_id,
        )

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)
