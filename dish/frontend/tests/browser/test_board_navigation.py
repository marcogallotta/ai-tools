from __future__ import annotations

import re

from playwright.sync_api import expect

from frontend.tests.browser.support.payloads import (
    BoardState,
    CardSpec,
    SECTION_RESEARCH,
    SECTION_VERIFY,
    TASK_ALPHA,
    TASK_BETA,
    TASK_DELTA,
    TASK_GAMMA,
)


def test_empty_registry_and_empty_sections_are_success_states(acceptance):
    acceptance.login()
    acceptance.wait_board()
    expect(acceptance.page.get_by_text("No incomplete tasks")).to_have_count(3)

    acceptance.runtime.board_state.sections = []
    acceptance.runtime.board_state.bump()
    acceptance.page.wait_for_timeout(1300)
    expect(acceptance.page.get_by_text("No active sections")).to_be_visible()
    acceptance.screenshot("empty-board")
    acceptance.assert_clean()


def test_populated_board_load_more_and_live_announcement(acceptance):
    acceptance.runtime.board_state.cards = [
        CardSpec(TASK_ALPHA, "Alpha soup"),
        CardSpec(TASK_BETA, "Beta curry"),
        CardSpec(TASK_GAMMA, "Gamma rice"),
        CardSpec(TASK_DELTA, "Delta noodles"),
    ]
    acceptance.login()
    acceptance.wait_board()
    research = acceptance.page.locator(f'.board-column[data-section-id="{SECTION_RESEARCH}"]')
    expect(research.locator(".task-card")).to_have_count(3)
    load = research.get_by_role("button", name="Load more")
    expect(load).to_be_visible()
    load.click()
    expect(research.locator(".task-card")).to_have_count(4)
    expect(research.get_by_role("button", name="Load more")).to_have_count(0)
    expect(acceptance.page.locator('[aria-live="polite"].sr-only')).to_contain_text("1 tasks added to Research Queue")
    acceptance.assert_clean()


def test_keyboard_navigation_and_horizontal_scroll(acceptance):
    acceptance.runtime.board_state.cards = [
        CardSpec(TASK_ALPHA, "Alpha soup", section_id=SECTION_RESEARCH),
        CardSpec(TASK_BETA, "Beta curry", section_id=SECTION_RESEARCH),
        CardSpec(TASK_GAMMA, "Gamma rice", section_id=SECTION_VERIFY),
    ]
    acceptance.login()
    acceptance.wait_board()

    alpha = acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')
    alpha.focus()
    alpha.press("ArrowDown")
    assert acceptance.page.evaluate("document.activeElement.dataset.taskId") == TASK_BETA
    acceptance.page.keyboard.press("ArrowRight")
    assert acceptance.page.evaluate("document.activeElement.dataset.taskId") == TASK_GAMMA

    board = acceptance.page.locator('[aria-label="Dish task board"]')
    board.focus()
    acceptance.page.evaluate("""() => {
      const scroller = document.querySelector('.board-scroller');
      window.__stage7ScrollCalls = 0;
      scroller.scrollBy = () => { window.__stage7ScrollCalls += 1; };
    }""")
    acceptance.page.keyboard.press("ArrowRight")
    assert acceptance.page.evaluate("window.__stage7ScrollCalls") == 1
    acceptance.assert_clean()


def test_minimum_desktop_viewport_has_no_page_level_horizontal_overflow(acceptance):
    acceptance.runtime.board_state = BoardState(cards=[
        CardSpec(TASK_ALPHA, "A very long dish title that still needs to remain contained inside the board card without expanding the document viewport"),
        CardSpec(TASK_BETA, "Beta curry", section_id=SECTION_VERIFY),
    ])
    acceptance.page.set_viewport_size({"width": 1024, "height": 768})
    acceptance.login()
    acceptance.wait_board()
    metrics = acceptance.page.evaluate("""() => ({
      viewport: document.documentElement.clientWidth,
      page: document.documentElement.scrollWidth,
      board: document.querySelector('.board-scroller').scrollWidth,
      boardViewport: document.querySelector('.board-scroller').clientWidth,
    })""")
    assert metrics["page"] <= metrics["viewport"] + 1
    assert metrics["board"] >= metrics["boardViewport"]
    acceptance.screenshot("minimum-desktop")
    acceptance.assert_clean()


def test_search_finds_unloaded_active_title_and_opens_canonical_detail_route(acceptance):
    acceptance.runtime.board_state.cards = [
        CardSpec(TASK_ALPHA, "Alpha soup"),
        CardSpec(TASK_BETA, "Beta curry"),
        CardSpec(TASK_GAMMA, "Gamma rice"),
        CardSpec(TASK_DELTA, "Delta noodles"),
    ]
    acceptance.login()
    acceptance.wait_board()

    research = acceptance.page.locator(f'.board-column[data-section-id="{SECTION_RESEARCH}"]')
    expect(research.locator(".task-card")).to_have_count(3)
    expect(acceptance.page.locator(f'[data-task-id="{TASK_DELTA}"]')).to_have_count(0)

    acceptance.page.get_by_label("Search active dishes").fill("NOOD")
    acceptance.page.get_by_role("button", name="Search", exact=True).click()
    result = acceptance.page.locator(".board-search__result").filter(has_text="Delta noodles")
    expect(result).to_be_visible()
    expect(result).to_contain_text("Cooking · Research Queue")
    assert acceptance.runtime.search_calls == ["NOOD"]

    result.click()
    expect(acceptance.page).to_have_url(re.compile(rf"/dishes/{TASK_DELTA}/delta-noodles$"))
    expect(acceptance.page.get_by_role("dialog").get_by_role("heading", name="Delta noodles")).to_be_visible()
    acceptance.assert_clean()
