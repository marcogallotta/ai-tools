from __future__ import annotations

from datetime import datetime, timezone

from playwright.sync_api import expect

from frontend.tests.browser.support.payloads import CardSpec, SECTION_VERIFY, TASK_ALPHA


def _verification_admin_payload() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "needs_you": 0,
            "human_review": 0,
            "recovery": 0,
            "workflow_queue": 1,
            "research": 0,
            "verification": 1,
            "system_activity": 0,
            "affected_dishes": 1,
        },
        "dishes": [{
            "task_id": TASK_ALPHA,
            "title": "Alpha soup",
            "section_label": "Verification Queue",
            "workflow_status": {"state": "no_active_operation"},
            "bucket": "workflow_queue",
            "attention": [{
                "code": "verification_required",
                "label": "Needs verification",
                "message": "This dish is waiting in the Verification queue.",
            }],
            "last_activity_at": None,
            "diagnostics": {"attention_codes": ["verification_required"]},
        }],
        "journal": [],
    }


def test_admin_shows_queue_work_without_inflating_needs_you(acceptance):
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup", section_id=SECTION_VERIFY)]
    acceptance.runtime.admin_payload = _verification_admin_payload()
    acceptance.login(return_path="/admin")
    admin = acceptance.page.locator('[aria-label="Dish administration"]')
    expect(admin).to_be_visible()
    expect(admin.get_by_role("heading", name="Nothing needs you right now")).to_be_visible()
    verification = admin.locator(".admin-summary-card").filter(has_text="Needs verification")
    expect(verification).to_contain_text("1")
    expect(admin.get_by_role("heading", name="Workflow queue")).to_be_visible()
    expect(admin.get_by_role("link", name="Alpha soup")).to_be_visible()
    expect(admin.locator(".admin-diagnostics[open]")).to_have_count(0)
    acceptance.screenshot("admin-workflow-queue")
    acceptance.assert_clean()
