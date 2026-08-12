from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from dish_pg.frontend_admin_query import AdminAuditFact, AdminHumanReviewFact, FrontendAdminFacts
from dish_pg.frontend_board_query import CardFact, SectionFact
from dish_service.frontend_admin import FrontendAdminConfig, FrontendAdminService

NOW = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
SECTION_ID = UUID("10000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("20000000-0000-0000-0000-000000000001")
TASK_ID = UUID("30000000-0000-0000-0000-000000000001")
REQUEST_ID = UUID("40000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000001")
REQUIREMENT_ID = UUID("60000000-0000-0000-0000-000000000001")
OPERATION_ID = UUID("70000000-0000-0000-0000-000000000001")


class FakeQuery:
    def capture(self, **_kwargs):
        raise AssertionError("present() test does not call capture")


def card(**overrides):
    values = dict(
        section_id=SECTION_ID,
        section_ordinal=1,
        task_id=TASK_ID,
        title="Miso soup",
        sort_title="miso soup",
        existence_state="ordinary",
        operation_kind=None,
        operation_phase=None,
        isolated=False,
        lease_attention=False,
        verification_attention=True,
        hold_active=False,
        recovery_required=False,
        abandonment_active=False,
        succession_active=False,
        projection_abnormal=False,
    )
    values.update(overrides)
    return CardFact(**values)


def test_admin_present_is_dish_first_and_human_review_counts_as_needs_you() -> None:
    question = "Choose whether to accept the material texture risk for service."
    facts = FrontendAdminFacts(
        sections=(SectionFact(SECTION_ID, 1, "Verification Queue", "verification_queue", PROJECT_ID, "Cooking", "active", "active"),),
        cards=(card(operation_kind="initial", operation_phase="held_human"),),
        events=(AdminAuditFact(EVENT_ID, REQUEST_ID, None, TASK_ID, None, "workflow_action_rejected", "agent", NOW),),
        human_reviews=(AdminHumanReviewFact(REQUIREMENT_ID, TASK_ID, OPERATION_ID, None, "human_review", question, NOW),),
        evaluation_time=NOW,
    )
    service = FrontendAdminService(
        FakeQuery(),
        environment="test",
        config=FrontendAdminConfig(projection_delay=timedelta(minutes=15)),
    )

    payload = service.present(facts)

    assert payload["summary"] == {
        "needs_you": 1,
        "human_review": 1,
        "recovery": 0,
        "workflow_queue": 0,
        "research": 0,
        "verification": 0,
        "system_activity": 0,
        "affected_dishes": 1,
    }
    dish = payload["dishes"][0]
    assert dish["task_id"] == str(TASK_ID)
    assert dish["bucket"] == "needs_you"
    assert dish["attention"][0]["label"] == "Waiting for your decision"
    assert dish["attention"][0]["message"] == question
    assert payload["journal"][0]["summary"] == "Workflow action was rejected"


def test_admin_system_activity_does_not_increase_needs_you() -> None:
    facts = FrontendAdminFacts(
        sections=(SectionFact(SECTION_ID, 1, "Operations", "operations", PROJECT_ID, "Cooking", "active", "active"),),
        cards=(card(verification_attention=False, hold_active=True),),
        events=(),
        evaluation_time=NOW,
    )
    service = FrontendAdminService(
        FakeQuery(), environment="test", config=FrontendAdminConfig(projection_delay=timedelta(minutes=15))
    )

    payload = service.present(facts)
    assert payload["summary"]["needs_you"] == 0
    assert payload["summary"]["system_activity"] == 1
    assert payload["dishes"][0]["bucket"] == "system_activity"


def test_admin_queue_roles_are_visible_without_inflating_needs_you() -> None:
    research_id = TASK_ID
    verification_id = UUID("30000000-0000-0000-0000-000000000002")
    facts = FrontendAdminFacts(
        sections=(
            SectionFact(SECTION_ID, 1, "Research Queue", "research_queue", PROJECT_ID, "Cooking", "active", "active"),
            SectionFact(UUID("10000000-0000-0000-0000-000000000002"), 2, "Verification Queue", "verification_queue", PROJECT_ID, "Cooking", "active", "active"),
        ),
        cards=(
            card(task_id=research_id, section_id=SECTION_ID, verification_attention=False),
            card(
                task_id=verification_id,
                section_id=UUID("10000000-0000-0000-0000-000000000002"),
                title="Tomato confit",
                sort_title="tomato confit",
                verification_attention=False,
            ),
        ),
        events=(),
        evaluation_time=NOW,
    )
    service = FrontendAdminService(
        FakeQuery(), environment="test", config=FrontendAdminConfig(projection_delay=timedelta(minutes=15))
    )

    payload = service.present(facts)

    assert payload["summary"] == {
        "needs_you": 0,
        "human_review": 0,
        "recovery": 0,
        "workflow_queue": 2,
        "research": 1,
        "verification": 1,
        "system_activity": 0,
        "affected_dishes": 2,
    }
    assert [dish["bucket"] for dish in payload["dishes"]] == ["workflow_queue", "workflow_queue"]
    assert {dish["attention"][0]["code"] for dish in payload["dishes"]} == {
        "research_required",
        "verification_required",
    }


def test_open_operation_takes_precedence_over_queue_fallback_as_system_activity() -> None:
    facts = FrontendAdminFacts(
        sections=(SectionFact(SECTION_ID, 1, "Research Queue", "research_queue", PROJECT_ID, "Cooking", "active", "active"),),
        cards=(card(verification_attention=False, operation_kind="initial", operation_phase="prepare_required"),),
        events=(),
        evaluation_time=NOW,
    )
    service = FrontendAdminService(
        FakeQuery(), environment="test", config=FrontendAdminConfig(projection_delay=timedelta(minutes=15))
    )

    payload = service.present(facts)

    assert payload["summary"]["workflow_queue"] == 0
    assert payload["summary"]["system_activity"] == 1
    assert payload["summary"]["affected_dishes"] == 1
    assert payload["dishes"][0]["bucket"] == "system_activity"
    assert payload["dishes"][0]["workflow_status"] == {
        "state": "active_operation",
        "operation": "Initial",
        "phase": "Prepare required",
    }
    assert payload["dishes"][0]["attention"] == []
    assert payload["dishes"][0]["diagnostics"]["attention_codes"] == []
