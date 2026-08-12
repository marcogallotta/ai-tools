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


def _active_operation_admin_payload() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "needs_you": 0,
            "human_review": 0,
            "recovery": 0,
            "workflow_queue": 0,
            "research": 0,
            "verification": 0,
            "system_activity": 1,
            "affected_dishes": 1,
        },
        "dishes": [{
            "task_id": TASK_ALPHA,
            "title": "Alpha soup",
            "section_label": "Verification Queue",
            "workflow_status": {"state": "active_operation", "operation": "Initial", "phase": "Prepare required"},
            "bucket": "system_activity",
            "attention": [],
            "last_activity_at": None,
            "diagnostics": {"attention_codes": []},
        }],
        "journal": [],
    }


def _human_review_admin_payload() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "needs_you": 1,
            "human_review": 1,
            "recovery": 0,
            "workflow_queue": 0,
            "research": 0,
            "verification": 0,
            "system_activity": 0,
            "affected_dishes": 1,
        },
        "dishes": [{
            "task_id": TASK_ALPHA,
            "title": "Alpha soup",
            "section_label": "Verification Queue",
            "workflow_status": {"state": "active_operation", "operation": "Verification", "phase": "Human review"},
            "bucket": "needs_you",
            "attention": [{
                "code": "verification_attention",
                "label": "Waiting for your decision",
                "message": "Accept the softer texture for tonight's service, or require another verification pass?",
            }],
            "last_activity_at": None,
            "diagnostics": {"attention_codes": ["verification_attention"]},
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


def test_admin_shows_active_operation_as_system_activity(acceptance):
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup", section_id=SECTION_VERIFY)]
    acceptance.runtime.admin_payload = _active_operation_admin_payload()
    acceptance.login(return_path="/admin")
    admin = acceptance.page.locator('[aria-label="Dish administration"]')
    expect(admin).to_be_visible()
    system = admin.locator(".admin-summary-card").filter(has_text="System handling it")
    expect(system).to_contain_text("1")
    group = admin.get_by_role("heading", name="System handling it").locator("..")
    expect(admin.get_by_role("link", name="Alpha soup")).to_be_visible()
    expect(admin.locator(".admin-dish__state")).to_have_text("Initial · Prepare required")
    expect(group).to_be_visible()
    acceptance.assert_clean()


def test_admin_shows_exact_human_review_decision_context(acceptance):
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup", section_id=SECTION_VERIFY)]
    acceptance.runtime.admin_payload = _human_review_admin_payload()
    acceptance.login(return_path="/admin")
    admin = acceptance.page.locator('[aria-label="Dish administration"]')
    expect(admin).to_be_visible()
    expect(admin.get_by_role("heading", name="1 dish needs you")).to_be_visible()
    review = admin.locator(".admin-attention--verification_attention")
    expect(review).to_contain_text("Waiting for your decision")
    expect(review).to_contain_text("Accept the softer texture for tonight's service, or require another verification pass?")
    expect(admin.locator(".admin-diagnostics[open]")).to_have_count(0)
    acceptance.screenshot("admin-human-review-context")
    acceptance.assert_clean()

def test_admin_inspect_any_dish_rejects_invalid_uuid_without_navigation(acceptance):
    acceptance.login(return_path="/admin")
    admin = acceptance.page.locator('[aria-label="Dish administration"]')
    form = admin.locator(".admin-inspect")
    expect(form.get_by_role("heading", name="Inspect any Dish")).to_be_visible()
    input_box = form.get_by_label("Dish UUID")
    input_box.fill("not-a-dish")
    form.get_by_role("button", name="Inspect").click()

    expect(form.get_by_role("alert")).to_have_text("Enter a valid non-zero Dish UUID.")
    expect(input_box).to_have_attribute("aria-invalid", "true")
    assert acceptance.page.url.split("?", 1)[0].endswith("/admin")
    acceptance.assert_clean()


def test_admin_inspect_any_dish_routes_resting_uuid_to_existing_detail(acceptance):
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup", section_id=SECTION_VERIFY)]
    acceptance.login(return_path="/admin")
    admin = acceptance.page.locator('[aria-label="Dish administration"]')
    admin.get_by_label("Dish UUID").fill(TASK_ALPHA.upper())
    admin.get_by_role("button", name="Inspect").click()

    acceptance.wait_detail()
    assert f"/dishes/{TASK_ALPHA}/alpha-soup" in acceptance.page.url
    assert acceptance.runtime.detail_calls == [TASK_ALPHA]
    acceptance.assert_clean()
