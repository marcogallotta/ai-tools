from __future__ import annotations

import re

from datetime import datetime, timezone

from playwright.sync_api import expect

from frontend.tests.browser.support.bridge import ORIGIN
from frontend.tests.browser.support.payloads import CardSpec, TASK_ALPHA
from frontend.tests.browser.support.runtime import PASSWORD


def _seed(acceptance) -> None:
    acceptance.runtime.board_state.cards = [CardSpec(TASK_ALPHA, "Alpha soup")]


def test_login_reload_session_metadata_and_logout(acceptance):
    _seed(acceptance)
    acceptance.goto("/")
    expect(acceptance.page).to_have_url(f"{ORIGIN}/login?return=rt1.Lw")
    expect(acceptance.page.locator("main")).to_have_count(1)
    acceptance.screenshot("login")

    acceptance.page.get_by_label("Shared password").fill(PASSWORD)
    acceptance.page.get_by_role("button", name="Sign in").click()
    expect(acceptance.page).to_have_url(f"{ORIGIN}/")
    acceptance.wait_board()
    acceptance.screenshot("board-authenticated")

    session = acceptance.page.evaluate("async () => (await fetch('/frontend/session')).json()")
    assert 604790 <= session["remaining_seconds"] <= 604800
    expires = datetime.fromisoformat(session["expires_at"])
    assert 604780 <= (expires - datetime.now(timezone.utc)).total_seconds() <= 604810
    assert len(session["csrf_proof"]) >= 22

    acceptance.page.reload(wait_until="domcontentloaded")
    acceptance.wait_board()
    acceptance.page.get_by_role("button", name="Sign out").click()
    expect(acceptance.page).to_have_url(f"{ORIGIN}/login")
    expect(acceptance.page.locator("main")).to_have_count(1)
    acceptance.assert_clean(allowed_http_errors=[(401, "/frontend/session")])


def test_expired_session_returns_to_original_deep_link(acceptance):
    _seed(acceptance)
    deep_link = f"/dishes/{TASK_ALPHA}/alpha-soup"
    acceptance.login(return_path=deep_link)
    acceptance.wait_detail()
    acceptance.runtime.auth.expire_all()

    acceptance.page.reload(wait_until="domcontentloaded")
    expect(acceptance.page).to_have_url(re.compile(r"/login\?return="))
    acceptance.page.get_by_label("Shared password").fill(PASSWORD)
    acceptance.page.get_by_role("button", name="Sign in").click()
    expect(acceptance.page).to_have_url(f"{ORIGIN}{deep_link}")
    acceptance.wait_detail()
    acceptance.assert_clean(allowed_http_errors=[(401, "/frontend/session")])


def test_replacement_login_reconciles_same_browser_tabs(acceptance):
    _seed(acceptance)
    acceptance.login()
    second = acceptance.page.context.new_page()
    second.goto(f"{ORIGIN}/", wait_until="domcontentloaded")
    second.locator('#app[data-shell-state="local-postgresql-board"]').wait_for()
    original_tokens = set(acceptance.runtime.auth.sessions)

    acceptance.goto("/login")
    acceptance.page.get_by_label("Shared password").fill(PASSWORD)
    with second.expect_event("framenavigated") as second_navigation:
        acceptance.page.get_by_role("button", name="Sign in").click()
    assert second_navigation.value == second.main_frame
    acceptance.wait_board()
    second.locator('#app[data-shell-state="local-postgresql-board"]').wait_for()

    new_tokens = set(acceptance.runtime.auth.sessions) - original_tokens
    assert len(new_tokens) == 1
    assert all(acceptance.runtime.auth.sessions[token].revoked for token in original_tokens)
    assert not acceptance.runtime.auth.sessions[new_tokens.pop()].revoked
    second.close()
    acceptance.assert_clean()


def test_logout_conceals_other_same_browser_tab(acceptance):
    _seed(acceptance)
    acceptance.login()
    second = acceptance.page.context.new_page()
    second.goto(f"{ORIGIN}/", wait_until="domcontentloaded")
    second.locator('#app[data-shell-state="local-postgresql-board"]').wait_for()

    with second.expect_event("framenavigated") as second_navigation:
        acceptance.page.get_by_role("button", name="Sign out").click()
    assert second_navigation.value == second.main_frame
    expect(second).to_have_url(f"{ORIGIN}/login")
    expect(second.locator("main")).to_have_count(1)
    expect(second.locator('[data-shell-state="local-postgresql-board"]')).to_have_count(0)
    second.close()
    acceptance.assert_clean(allowed_http_errors=[(401, "/frontend/session")])


def test_admin_is_an_authenticated_return_target(acceptance):
    acceptance.goto("/admin")
    expect(acceptance.page).to_have_url(re.compile(r"/login\?return="))
    acceptance.page.get_by_label("Shared password").fill(PASSWORD)
    acceptance.page.get_by_role("button", name="Sign in").click()
    expect(acceptance.page).to_have_url(f"{ORIGIN}/admin")
    acceptance.page.locator('[aria-label="Dish administration"]').wait_for()
    acceptance.assert_clean(allowed_http_errors=[(401, "/frontend/session")])
