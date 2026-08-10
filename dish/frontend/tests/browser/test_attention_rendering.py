from __future__ import annotations

from playwright.sync_api import expect

from frontend.tests.browser.support.payloads import (
    ATTENTION_ORDER,
    CardSpec,
    SECTION_READY,
    SECTION_RESEARCH,
    SECTION_VERIFY,
    TASK_ALPHA,
    TASK_BETA,
    TASK_DELTA,
    TASK_EPSILON,
    TASK_ETA,
    TASK_GAMMA,
    TASK_IOTA,
    TASK_THETA,
    TASK_ZETA,
)

_TASKS = [TASK_ALPHA, TASK_BETA, TASK_GAMMA, TASK_DELTA, TASK_EPSILON, TASK_ZETA, TASK_ETA, TASK_THETA]
_SECTIONS = [SECTION_RESEARCH, SECTION_VERIFY, SECTION_READY]


def test_every_attention_category_and_grouped_banner(acceptance):
    cards = [
        CardSpec(task_id, f"Attention {code}", section_id=_SECTIONS[index % 3], attention=(code,))
        for index, (task_id, code) in enumerate(zip(_TASKS, ATTENTION_ORDER, strict=True))
    ]
    cards.append(CardSpec(TASK_IOTA, "Second lease warning", section_id=SECTION_READY, attention=("lease_attention",)))
    acceptance.runtime.board_state.cards = cards
    acceptance.login()
    acceptance.wait_board()

    for code in ATTENTION_ORDER:
        expect(acceptance.page.locator(f'[data-notice-code="{code}"]')).to_be_visible()
    expect(acceptance.page.get_by_text("Lease needs attention — 2 tasks")).to_be_visible()
    acceptance.screenshot("attention-categories")
    acceptance.assert_clean()


def test_recovered_attention_is_removed_after_refresh(acceptance):
    card = CardSpec(TASK_ALPHA, "Recovering soup", attention=("recovery_required",))
    acceptance.runtime.board_state.cards = [card]
    acceptance.login()
    expect(acceptance.page.locator('[data-notice-code="recovery_required"]')).to_be_visible()

    card.attention = ()
    acceptance.runtime.board_state.bump()
    acceptance.page.wait_for_timeout(1400)
    expect(acceptance.page.locator('[data-notice-code="recovery_required"]')).to_have_count(0)
    expect(acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"] .task-card__attention')).to_be_empty()
    acceptance.assert_clean()


def test_long_content_projection_warning_and_collapsed_diagnostics(acceptance):
    body = """
      <h2>Canonical overview</h2>
      <p>This intentionally long canonical paragraph repeats operationally useful detail without exposing raw authority state. """ + ("Long content. " * 120) + """</p>
      <details class="canonical-process-record"><summary>Process Record</summary><p>Collapsed provenance.</p></details>
      <p>Visual reference — <a href="https://www.justonecookbook.com/homemade-miso-soup/" target="_blank" rel="noopener noreferrer">Just One Cookbook</a></p>
    """
    card = CardSpec(
        TASK_ALPHA,
        "Long canonical soup",
        attention=("projection_abnormal",),
        body_html=body,
        projection_state="drifted",
        projection_message="The observed Asana projection no longer matches the current canonical state.",
    )
    acceptance.runtime.board_state.cards = [card]
    acceptance.login()
    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    acceptance.wait_detail()

    dialog = acceptance.page.get_by_role("dialog")
    expect(dialog.locator(".canonical-process-record")).not_to_have_attribute("open", "")
    expect(dialog.locator(".detail-technical")).not_to_have_attribute("open", "")
    dialog.locator(".detail-technical summary").click()
    expect(dialog.get_by_text("Projection — drifted")).to_be_visible()
    link = dialog.get_by_role("link", name="Just One Cookbook")
    expect(link).to_have_attribute("href", "https://www.justonecookbook.com/homemade-miso-soup/")
    acceptance.screenshot("long-detail")
    acceptance.assert_clean()


def test_safe_plain_text_fallback_is_inert(acceptance):
    dangerous_text = '<script>window.__stage7Injected = true</script><a href="javascript:alert(1)">bad</a>'
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Fallback soup", fallback_text=dangerous_text)]
    acceptance.login()
    acceptance.page.locator(f'[data-task-id="{TASK_ALPHA}"]').click()
    acceptance.wait_detail()

    dialog = acceptance.page.get_by_role("dialog")
    expect(dialog.locator('.detail-content[data-render-mode="fallback"]')).to_be_visible()
    expect(dialog.locator("script")).to_have_count(0)
    expect(dialog.locator('a[href^="javascript:"]')).to_have_count(0)
    assert acceptance.page.evaluate("window.__stage7Injected === true") is False
    expect(acceptance.page.locator('[data-notice-code="render_rejected"]')).to_be_visible()
    acceptance.assert_clean()
