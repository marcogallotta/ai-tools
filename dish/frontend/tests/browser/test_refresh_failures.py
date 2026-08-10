from __future__ import annotations

import re

from playwright.sync_api import expect

from frontend.tests.browser.support.payloads import CardSpec, TASK_ALPHA, TASK_BETA, TASK_DELTA, TASK_GAMMA


def _cards(acceptance, count: int = 1) -> None:
    available = [
        CardSpec(TASK_ALPHA, "Alpha soup"),
        CardSpec(TASK_BETA, "Beta curry"),
        CardSpec(TASK_GAMMA, "Gamma rice"),
        CardSpec(TASK_DELTA, "Delta noodles"),
    ]
    acceptance.runtime.board_state.cards = available[:count]


def test_initial_network_failure_retries_without_reload(acceptance):
    _cards(acceptance)
    acceptance.bridge.fail_transport("/frontend/board")
    acceptance.login()
    expect(acceptance.page.get_by_role("heading", name="Board not loaded")).to_be_visible()
    assert any("/frontend/board" in item for item in acceptance.audit.request_failures)

    acceptance.page.get_by_role("button", name="Retry board load").click()
    acceptance.wait_board()
    expect(acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')).to_be_visible()
    assert acceptance.audit.console_errors == []
    assert acceptance.audit.page_errors == []


def test_background_service_failure_keeps_last_safe_board(acceptance):
    _cards(acceptance)
    acceptance.login()
    acceptance.wait_board()
    acceptance.runtime.board_failure = "unavailable"
    acceptance.page.wait_for_timeout(1150)

    expect(acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')).to_be_visible()
    expect(acceptance.page.locator('[data-notice-code="service_unavailable"]')).to_be_visible()
    acceptance.assert_clean(allowed_http_errors=[(503, "/frontend/board")])


def test_contract_mismatch_keeps_last_safe_board_and_requires_reload(acceptance):
    _cards(acceptance)
    acceptance.login()
    acceptance.runtime.malformed_board = True
    acceptance.page.wait_for_timeout(1150)

    expect(acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')).to_be_visible()
    notice = acceptance.page.locator('[data-notice-code="contract_mismatch"]')
    expect(notice).to_be_visible()
    expect(notice.get_by_role("button", name="Reload page")).to_be_visible()
    acceptance.assert_clean()


def test_stale_cursor_resets_only_column_then_can_retry(acceptance):
    _cards(acceptance, 4)
    acceptance.login()
    research = acceptance.page.locator(".board-column").filter(has_text="Research Queue")
    acceptance.runtime.continuation_failure = "stale"
    research.get_by_role("button", name="Load more").click()
    acceptance.page.wait_for_timeout(350)
    expect(research.locator(".task-card")).to_have_count(3)
    expect(research.get_by_role("button", name="Load more")).to_be_visible()

    research.get_by_role("button", name="Load more").click()
    expect(research.locator(".task-card")).to_have_count(4)
    acceptance.assert_clean(allowed_http_errors=[(409, "/frontend/sections/")])


def test_missing_and_temporarily_unavailable_detail_fail_closed(acceptance):
    _cards(acceptance)
    acceptance.login()
    acceptance.runtime.detail_failures[TASK_ALPHA] = "unavailable"
    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    expect(acceptance.page.get_by_role("dialog")).to_have_count(0)
    expect(acceptance.page.locator('[data-notice-code="service_unavailable"]')).to_be_visible()

    acceptance.runtime.detail_failures[TASK_ALPHA] = "not_found"
    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    expect(acceptance.page).to_have_url(re.compile(r"/$"))
    expect(acceptance.page.locator('[data-notice-code="task_not_found"]')).to_be_visible()
    acceptance.assert_clean(allowed_http_errors=[
        (503, f"/frontend/tasks/{TASK_ALPHA}"),
        (404, f"/frontend/tasks/{TASK_ALPHA}"),
    ])


def test_session_service_failure_conceals_protected_content(acceptance):
    _cards(acceptance)
    acceptance.login()
    acceptance.runtime.auth.validation_failure = "session_unavailable"
    acceptance.page.evaluate("window.dispatchEvent(new Event('pageshow'))")
    expect(acceptance.page).to_have_url(re.compile(r"/login\?return="))
    expect(acceptance.page.locator('#app:not([hidden])')).to_have_attribute("data-shell-state", "login")
    acceptance.assert_clean(allowed_http_errors=[(503, "/frontend/session")])
