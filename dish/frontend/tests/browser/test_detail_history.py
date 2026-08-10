from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from frontend.tests.browser.support.bridge import ORIGIN
from frontend.tests.browser.support.payloads import (
    CardSpec,
    SECTION_RESEARCH,
    SECTION_VERIFY,
    TASK_ALPHA,
    TASK_BETA,
)


def _seed(acceptance) -> None:
    acceptance.runtime.board_state.cards = [
        CardSpec(TASK_ALPHA, "Alpha soup", advisory_code="workflow.research_required", advisory_message="Research this dish before continuing."),
        CardSpec(TASK_BETA, "Beta curry", section_id=SECTION_VERIFY),
    ]


def test_open_close_focus_restore_and_refresh(acceptance):
    _seed(acceptance)
    acceptance.login()
    card = acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')
    card.focus()
    card.click()
    acceptance.wait_detail()
    expect(acceptance.page).to_have_url(re.compile(rf"/dishes/{TASK_ALPHA}/alpha-soup$"))
    expect(acceptance.page.get_by_text("What needs to happen next")).to_be_visible()
    acceptance.page.reload(wait_until="domcontentloaded")
    acceptance.wait_detail()
    acceptance.page.get_by_role("button", name="Close task detail").click()
    acceptance.wait_board()
    assert acceptance.page.evaluate("document.activeElement.dataset.taskId") == TASK_ALPHA
    acceptance.assert_clean()


def test_direct_deep_link_and_back_forward_history(acceptance):
    _seed(acceptance)
    deep = f"/dishes/{TASK_ALPHA}/alpha-soup"
    acceptance.login(return_path=deep)
    acceptance.wait_detail()
    expect(acceptance.page).to_have_url(f"{ORIGIN}{deep}")
    acceptance.page.get_by_role("button", name="Close task detail").click()
    expect(acceptance.page).to_have_url(f"{ORIGIN}/")

    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    acceptance.wait_detail()
    acceptance.page.go_back(wait_until="domcontentloaded")
    acceptance.wait_board()
    expect(acceptance.page.get_by_role("dialog")).to_have_count(0)
    acceptance.page.go_forward(wait_until="domcontentloaded")
    acceptance.wait_detail()
    acceptance.assert_clean()


def test_moved_task_reconciles_to_new_section(acceptance):
    _seed(acceptance)
    acceptance.login()
    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    acceptance.wait_detail()
    acceptance.runtime.board_state.cards[0].section_id = SECTION_VERIFY
    acceptance.runtime.board_state.bump()
    acceptance.page.wait_for_timeout(1400)

    verify = acceptance.page.locator(f'.board-column[data-section-id="{SECTION_VERIFY}"]')
    expect(verify.locator(f'[data-task-id="{TASK_ALPHA}"]')).to_have_count(1)
    expect(acceptance.page.get_by_role("dialog").get_by_text("Cooking / Verification Queue")).to_be_visible()
    acceptance.assert_clean()


@pytest.mark.parametrize("lifecycle", ["completed", "retired"])
def test_completed_or_retired_task_closes_detail_and_reconciles(acceptance, lifecycle):
    _seed(acceptance)
    acceptance.login()
    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    acceptance.wait_detail()
    assert lifecycle in {"completed", "retired"}
    acceptance.runtime.detail_failures[TASK_ALPHA] = "ineligible"
    acceptance.runtime.board_state.cards = [card for card in acceptance.runtime.board_state.cards if card.task_id != TASK_ALPHA]
    acceptance.runtime.board_state.bump()
    acceptance.page.wait_for_timeout(1400)

    expect(acceptance.page.get_by_role("dialog")).to_have_count(0)
    expect(acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')).to_have_count(0)
    expect(acceptance.page.locator('[data-notice-code="task_ineligible"]')).to_be_visible()
    acceptance.assert_clean(allowed_http_errors=[(409, f"/frontend/tasks/{TASK_ALPHA}")])
