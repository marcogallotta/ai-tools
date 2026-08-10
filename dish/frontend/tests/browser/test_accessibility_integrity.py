from __future__ import annotations

from playwright.sync_api import expect

from frontend.tests.browser.support.payloads import CardSpec, TASK_ALPHA, TASK_BETA


def test_landmarks_focus_dialog_and_storage_security(acceptance):
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup"), CardSpec(TASK_BETA, "Beta curry")]
    acceptance.goto("/")
    expect(acceptance.page.locator("main")).to_have_count(1)
    expect(acceptance.page.get_by_label("Shared password")).to_be_focused()
    acceptance.page.get_by_label("Shared password").fill("correct horse battery staple")
    acceptance.page.get_by_role("button", name="Sign in").click()
    acceptance.wait_board()
    expect(acceptance.page.locator("main")).to_have_count(1)

    first = acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]')
    first.focus()
    focus_style = first.evaluate("node => { const s=getComputedStyle(node); return {outline:s.outlineStyle, width:s.outlineWidth}; }")
    assert focus_style["outline"] != "none"
    assert focus_style["width"] != "0px"
    first.click()
    dialog = acceptance.page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    labelled_by = dialog.get_attribute("aria-labelledby")
    assert labelled_by
    expect(acceptance.page.locator(f"#{labelled_by}")).to_have_text("Alpha soup")
    expect(acceptance.page.get_by_role("button", name="Close task detail")).to_be_focused()

    acceptance.page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert acceptance.page.evaluate("document.activeElement.dataset.taskId") == TASK_ALPHA
    storage = acceptance.page.evaluate("""async () => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      documentCookie: document.cookie,
      serviceWorkers: 'serviceWorker' in navigator ? (await navigator.serviceWorker.getRegistrations()).length : 0,
    })""")
    assert storage == {"local": [], "session": [], "documentCookie": "", "serviceWorkers": 0}
    acceptance.assert_clean()


def test_basic_accessibility_names_and_live_regions(acceptance):
    acceptance.runtime.board_state.cards = [
        CardSpec(TASK_ALPHA, "Alpha soup", attention=("lease_attention",)),
        CardSpec(TASK_BETA, "Beta curry", attention=("lease_attention",)),
    ]
    acceptance.login()
    acceptance.wait_board()
    expect(acceptance.page.get_by_role("navigation", name="Primary")).to_be_visible()
    expect(acceptance.page.locator('[aria-label="Dish task board"]')).to_have_attribute("aria-busy", "false")
    expect(acceptance.page.locator('[data-notice-code="lease_attention"]')).to_have_attribute("aria-live", "polite")
    expect(acceptance.page.get_by_text("Lease needs attention — 2 tasks")).to_be_visible()
    names = acceptance.page.locator(".task-card").evaluate_all("nodes => nodes.map(node => node.getAttribute('aria-label'))")
    assert all(name and "Lease needs attention" in name for name in names)
    acceptance.assert_clean()


def test_console_network_redirect_and_layout_audit_is_clean(acceptance):
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup")]
    acceptance.login()
    acceptance.wait_board()
    acceptance.screenshot("clean-production-board")
    acceptance.assert_clean()
    assert all(status < 400 for status, _url in acceptance.audit.responses)
    assert all(location.startswith("/") or location == "" for _status, _url, location in acceptance.audit.redirects)
    metrics = acceptance.page.evaluate("""() => ({
      viewportWidth: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      mainCount: document.querySelectorAll('main').length,
      dialogCount: document.querySelectorAll('[role=dialog]').length,
    })""")
    assert metrics["documentWidth"] <= metrics["viewportWidth"] + 1
    assert metrics["mainCount"] == 1
    assert metrics["dialogCount"] == 0
