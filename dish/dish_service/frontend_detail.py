"""Stage 4 read-only task-detail service over captured PostgreSQL facts."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta

from dish_pg.frontend_detail_query import DetailFacts, FrontendDetailQuery, TaskDetailIneligible
from dish_service.frontend_advisory import workflow_advisory
from dish_service.frontend_contract import (
    ATTENTION_BY_CODE,
    ATTENTION_PRESENTATIONS,
    RENDER_REJECTED_NOTICE,
    operation_status,
)
from dish_service.frontend_disclosure import detail_disclosures
from dish_service.frontend_projection import abnormal_projection
from dish_service.frontend_renderer import (
    DetailCapacityExceeded,
    RenderConfig,
    RenderRejected,
    plain_text_fallback,
    render_body,
)
from dish_service.frontend_tokens import route_identity

_TASK_ROUTE_RE = re.compile(r"(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ATTENTION_TO_DISCLOSURE = {
    "lease_attention": "lease",
    "verification_attention": "verification",
    "hold_active": "hold",
    "recovery_required": "recovery",
    "abandonment_active": "abandonment",
    "succession_active": "succession",
}


class TaskNotFound(LookupError):
    """Task UUID route does not identify a known bounded candidate."""


@dataclass(frozen=True, slots=True)
class FrontendDetailConfig:
    projection_delay: timedelta
    max_route_candidates: int = 5000
    max_disclosures: int = 20
    max_response_bytes: int = 500_000
    renderer: RenderConfig = field(default_factory=RenderConfig)

    def __post_init__(self) -> None:
        if self.projection_delay <= timedelta(0):
            raise ValueError("projection_delay must be positive")
        if min(self.max_route_candidates, self.max_disclosures, self.max_response_bytes) <= 0:
            raise ValueError("detail bounds must be positive")


class FrontendDetailService:
    """Resolve a canonical stored Dish UUID and present one closed Stage 4 DTO."""

    def __init__(
        self,
        query: FrontendDetailQuery,
        *,
        environment: str,
        token_secret: bytes,
        config: FrontendDetailConfig,
    ) -> None:
        self.query = query
        self.environment = environment
        self.token_secret = token_secret
        self.config = config

    def capture(self, task_route_id: str) -> DetailFacts:
        if not isinstance(task_route_id, str) or _TASK_ROUTE_RE.fullmatch(task_route_id) is None:
            raise TaskNotFound("task route identity is invalid")
        candidates = self.query.route_candidate_ids(limit=self.config.max_route_candidates)
        if len(candidates) > self.config.max_route_candidates:
            raise DetailCapacityExceeded("task route candidate bound exceeded")
        task_id = next(
            (
                candidate
                for candidate in candidates
                if self._task_route(candidate) == task_route_id
            ),
            None,
        )
        if task_id is None:
            raise TaskNotFound("task route identity was not found")
        return self.query.capture(task_id=task_id, projection_delay=self.config.projection_delay)

    def present(self, facts: DetailFacts) -> dict[str, object]:
        task_route_id = self._task_route(facts.task_id)
        body, render_notice = self._body(facts.body)
        attention_codes = self._attention_codes(facts)
        disclosures = detail_disclosures(facts)
        if len(disclosures) > self.config.max_disclosures:
            raise DetailCapacityExceeded("detail disclosure bound exceeded")
        projection = abnormal_projection(facts.projection)
        self._require_support(attention_codes, disclosures, projection)
        notices = [self._notice(code, task_route_id) for code in attention_codes]
        if render_notice:
            notices.append(self._render_notice(task_route_id))
        payload: dict[str, object] = {
            "task_id": task_route_id,
            "title": facts.title,
            "section_label": facts.section_label,
            # Destination authority remains unresolved. Do not derive it from body text.
            "destination_label": None,
            "workflow_status": operation_status(facts.operation_kind, facts.operation_phase),
            "attention_codes": attention_codes,
            "body_presentation": body,
            "disclosures": disclosures,
            "advisory": workflow_advisory(
                facts.operation_phase,
                section_workflow_role=facts.section_workflow_role,
            ),
            "projection": projection,
            "notices": notices,
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > self.config.max_response_bytes:
            raise DetailCapacityExceeded("detail response exceeds the configured bound")
        return payload

    def _body(self, source: str) -> tuple[dict[str, str], bool]:
        try:
            return render_body(source, config=self.config.renderer), False
        except RenderRejected:
            return plain_text_fallback(source, config=self.config.renderer), True

    @staticmethod
    def _attention_codes(facts: DetailFacts) -> list[str]:
        active = {
            "isolated": facts.isolated,
            "lease_attention": facts.lease_attention,
            "verification_attention": facts.verification_attention,
            "hold_active": facts.hold_active,
            "recovery_required": facts.recovery_required,
            "abandonment_active": facts.abandonment_active,
            "succession_active": facts.succession_active,
            "projection_abnormal": facts.projection_abnormal,
        }
        return [item.code for item in ATTENTION_PRESENTATIONS if active[item.code]]

    @staticmethod
    def _require_support(
        attention_codes: list[str],
        disclosures: list[dict[str, str]],
        projection: dict[str, str] | None,
    ) -> None:
        available = {item["code"] for item in disclosures}
        for code in attention_codes:
            category = _ATTENTION_TO_DISCLOSURE.get(code)
            if category and category not in available:
                raise ValueError(f"attention disclosure missing for {code}")
        if "projection_abnormal" in attention_codes and projection is None:
            raise ValueError("projection attention requires an abnormal projection object")

    def _task_route(self, task_id) -> str:
        return route_identity(
            secret=self.token_secret,
            environment=self.environment,
            kind="task",
            object_id=task_id,
        )

    @staticmethod
    def _notice(code: str, task_route_id: str) -> dict[str, object]:
        item = ATTENTION_BY_CODE[code]
        return {
            "code": code,
            "severity": item.severity,
            "message": item.message,
            "target": {"type": "task", "route_identity": task_route_id},
        }

    @staticmethod
    def _render_notice(task_route_id: str) -> dict[str, object]:
        item = RENDER_REJECTED_NOTICE
        return {
            "code": item.code,
            "severity": item.severity,
            "message": item.message,
            "target": {"type": "task", "route_identity": task_route_id},
        }


__all__ = [
    "DetailCapacityExceeded",
    "FrontendDetailConfig",
    "FrontendDetailService",
    "TaskDetailIneligible",
    "TaskNotFound",
]
