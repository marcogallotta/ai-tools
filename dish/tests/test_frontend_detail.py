from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from dish_pg.frontend_detail_query import (
    AbandonmentFact,
    DetailFacts,
    HoldFact,
    LeaseFact,
    VerificationFact,
)
from dish_pg.frontend_projection_query import ProjectionFact
from dish_service.frontend_detail import FrontendDetailConfig, FrontendDetailService, TaskNotFound
from dish_service.frontend_renderer import DetailCapacityExceeded, RenderConfig, render_body
from dish_service.frontend_tokens import route_identity

SECRET = b"stage-4-detail-test-secret-is-at-least-32-bytes"
TASK_ID = UUID("00000000-0000-0000-0000-000000000123")
NOW = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)


class FakeQuery:
    def __init__(self, facts: DetailFacts, *, extras: int = 0) -> None:
        self.facts = facts
        self.extras = extras

    def route_candidate_ids(self, *, limit: int):
        values = [TASK_ID] + [UUID(int=index + 1000) for index in range(self.extras)]
        return tuple(values[: limit + 1])

    def capture(self, *, task_id, projection_delay):
        assert task_id == TASK_ID
        assert projection_delay == timedelta(minutes=15)
        return self.facts


def facts(**overrides) -> DetailFacts:
    values = dict(
        task_id=TASK_ID,
        evaluation_time=NOW,
        title="[ready] Exact task",
        body="# Canonical\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n[slash](\\\\evil.example)\n\n[external](https://evil.example)\n\n[local](docs/help)\n",
        existence_state="isolated",
        section_label="Research Queue",
        project_label="Cooking",
        operation_kind="initial",
        operation_phase="prepare_required",
        isolated=True,
        lease_attention=True,
        verification_attention=True,
        hold_active=True,
        recovery_required=True,
        abandonment_active=True,
        succession_active=True,
        projection_abnormal=True,
        lease=LeaseFact("expired", "constructor", NOW - timedelta(minutes=1)),
        verification=VerificationFact("open", "recorded outcome"),
        holds=(HoldFact("evidence", "open"),),
        abandonment=AbandonmentFact("blocked"),
        projection=ProjectionFact(NOW, None, None, None),
    )
    values.update(overrides)
    return DetailFacts(**values)


def service(current: DetailFacts, *, max_candidates: int = 20) -> FrontendDetailService:
    return FrontendDetailService(
        FakeQuery(current),
        environment="test",
        token_secret=SECRET,
        config=FrontendDetailConfig(
            projection_delay=timedelta(minutes=15),
            max_route_candidates=max_candidates,
        ),
    )


def test_detail_presentation_is_closed_non_authorizing_and_uses_canonical_dish_uuid() -> None:
    current = facts()
    current_service = service(current)
    route = route_identity(secret=SECRET, environment="test", kind="task", object_id=TASK_ID)
    captured = current_service.capture(route)
    payload = current_service.present(captured)

    assert payload["task_id"] == route
    assert payload["attention_codes"][0] == "isolated"
    assert payload["body_presentation"]["state"] == "sanitized_html"
    assert "<script>" not in payload["body_presentation"]["html"]
    assert "javascript:" not in payload["body_presentation"]["html"]
    assert "evil.example" not in payload["body_presentation"]["html"]
    assert 'href="/docs/help"' in payload["body_presentation"]["html"]
    assert [item["code"] for item in payload["disclosures"]] == [
        "lease", "verification", "hold", "recovery", "abandonment", "succession"
    ]
    assert payload["advisory"]["invokable_by_frontend"] is False
    assert payload["projection"]["state"] == "drifted"
    assert payload["destination_label"] is None
    assert "legal_actions" not in payload
    assert "allowed_actions" not in payload
    assert payload["task_id"] == str(TASK_ID)


def test_verification_attention_without_cycle_gets_factual_disclosure() -> None:
    current = facts(verification=None)
    payload = service(current).present(current)
    verification = [item for item in payload["disclosures"] if item["code"] == "verification"]
    assert verification == [{
        "code": "verification",
        "label": "Verification",
        "detail": "Verification is awaiting human review.",
    }]


def test_renderer_fallback_is_inert_and_has_exact_notice() -> None:
    current = facts(
        body="```\nunclosed\n",
        isolated=False,
        lease_attention=False,
        verification_attention=False,
        hold_active=False,
        recovery_required=False,
        abandonment_active=False,
        succession_active=False,
        projection_abnormal=False,
        lease=None,
        verification=None,
        holds=(),
        abandonment=None,
        projection=ProjectionFact(None, None, None, None),
    )
    payload = service(current).present(current)
    assert payload["body_presentation"] == {"state": "plain_text_fallback", "text": current.body}
    assert [notice["code"] for notice in payload["notices"]] == ["render_rejected"]


def test_route_resolution_is_bounded_and_unknown_routes_fail_closed() -> None:
    current = facts()
    current_service = service(current, max_candidates=1)
    with pytest.raises(TaskNotFound):
        current_service.capture("12345678-1234-5678-1234-567812345679")

    overflowing = FrontendDetailService(
        FakeQuery(current, extras=2),
        environment="test",
        token_secret=SECRET,
        config=FrontendDetailConfig(projection_delay=timedelta(minutes=15), max_route_candidates=1),
    )
    with pytest.raises(DetailCapacityExceeded, match="candidate"):
        overflowing.capture(route_identity(secret=SECRET, environment="test", kind="task", object_id=TASK_ID))


def test_renderer_enforces_input_capacity() -> None:
    with pytest.raises(DetailCapacityExceeded):
        render_body("x" * 11, config=RenderConfig(max_body_chars=10))


def test_renderer_collapses_canonical_process_record_only() -> None:
    canonical = "WHY COOK IT\nUseful context.\n\n---\n## PROCESS RECORD\n### Planning brief\nStatus: ready\n"
    rendered = render_body(canonical, config=RenderConfig())["html"]
    assert "Useful context." in rendered
    assert '<details class="canonical-process-record">' in rendered
    assert "Process record and technical details" in rendered
    assert "Planning brief" in rendered
    assert rendered.index("Useful context.") < rendered.index("<details")

    generic = render_body("Visible\n\n---\n\nStill visible", config=RenderConfig())["html"]
    assert "canonical-process-record" not in generic
    assert "<hr>" in generic
