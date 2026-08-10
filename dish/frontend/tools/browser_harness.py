from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

from stage5_cursor_harness import assert_stage5_repeated_invalid_cursors


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCREENSHOTS = ROOT / "screenshots"
STYLE_FILES = (
    "tokens.css",
    "base.css",
    "layout.css",
    "components.css",
    "board.css",
    "detail.css",
    "notices.css",
    "admin.css",
    "review.css",
)
IMPORT_RE = re.compile(r'^import\s+.*?\s+from\s+["\'](.+?)["\'];?\s*$', re.MULTILINE)


def browser_executable() -> str:
    configured = os.environ.get("CHROMIUM_BIN")
    executable = configured or shutil.which("chromium")
    if not executable:
        raise RuntimeError("Chromium executable is required")
    return executable


def module_bundle(entries: tuple[Path, ...]) -> str:
    visited: set[Path] = set()
    ordered: list[str] = []

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        source = resolved.read_text(encoding="utf-8")
        for match in IMPORT_RE.finditer(source):
            dependency = match.group(1)
            if dependency.startswith("."):
                visit((resolved.parent / dependency).with_suffix(".js") if not dependency.endswith(".js") else resolved.parent / dependency)
        visited.add(resolved)
        source = IMPORT_RE.sub("", source)
        source = re.sub(r"^export\s+", "", source, flags=re.MULTILINE)
        source = source.replace("\nboot();\n", "\n")
        try:
            label = resolved.relative_to(ROOT)
        except ValueError:
            label = resolved.name
        ordered.append(f"\n// {label}\n{source}")

    for entry in entries:
        visit(entry)
    return "\n".join(ordered)


def prepare_page(page, view: str, task_id: str | None = None, review_mode: bool = False) -> None:
    page.set_content('<div id="app" class="app-root"></div>')
    css = "\n".join((SRC / "styles" / name).read_text(encoding="utf-8") for name in STYLE_FILES)
    page.add_style_tag(content=css)
    bundle = module_bundle((
        SRC / "js" / "shell" / "login-shell.js",
        SRC / "js" / "prototype" / "prototype-app.js",
    ))
    page.add_script_tag(content=bundle)
    if view == "login":
        page.evaluate("renderLoginShell(document.querySelector('#app'))")
    elif view in {"zero", "loading", "initial-error", "last-safe"}:
        page.evaluate(f"renderFixturePrototype(document.querySelector('#app'), '{view}', null, {{ reviewMode: {str(review_mode).lower()} }})")
    else:
        page.evaluate(
            "args => renderFixturePrototype(document.querySelector('#app'), args.scenario, args.taskId, { reviewMode: args.reviewMode })",
            {"scenario": view if view == "extreme" else "board", "taskId": task_id, "reviewMode": review_mode},
        )


def assert_shells(browser) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    prepare_page(page, "login")
    page.locator('#app[data-shell-state="login"]').wait_for()
    assert page.locator("main").count() == 1
    assert page.get_by_label("Shared password").is_disabled()
    page.evaluate("renderLogoutPendingShell(document.querySelector('#app'), { onRetry: () => {} })")
    assert page.locator("main").count() == 1
    assert page.get_by_role("button", name="Retry sign out").is_visible()
    prepare_page(page, "app")
    page.locator('#app[data-shell-state="fixture-board"]').wait_for()
    assert page.locator("main").count() == 1
    assert page.locator("#app").get_attribute("aria-live") is None
    assert page.locator('[aria-label="Dish task board"]').is_visible()
    assert page.locator(".board-column").first.get_attribute("aria-busy") == "false"
    assert page.get_by_role("button", name="Load more").is_visible()
    assert page.get_by_text(re.compile("Lease needs attention — 2 tasks")).is_visible()
    first = page.locator('[data-task-id="task-aubergine"]')
    first.focus()
    first.press("ArrowDown")
    assert page.evaluate("document.activeElement.dataset.taskId") == "task-biryani"
    page.get_by_role("button", name=re.compile("Chicken biryani")).click()
    assert page.get_by_role("dialog").is_visible()
    assert page.get_by_text("What needs to happen next").is_visible()
    assert page.locator(".detail-content").evaluate("node => getComputedStyle(node).unicodeBidi") == "isolate"
    assert page.locator("body").get_attribute("data-prototype-route") == "/task/task-biryani"
    page.keyboard.press("Escape")
    assert page.get_by_role("dialog").count() == 0
    assert page.evaluate("document.activeElement.dataset.taskId") == "task-biryani"
    page.emulate_media(reduced_motion="reduce")
    page.locator('[aria-label="Dish task board"]').focus()
    page.evaluate("""() => {
      const scroller = document.querySelector('.board-scroller');
      scroller.scrollBy = options => { window.__dishScrollBehavior = options.behavior; };
    }""")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("window.__dishScrollBehavior") == "auto"
    calls = page.evaluate("""() => {
      const host = document.querySelector('[aria-label="Dish task board"]');
      const board = fixtureForScenario('board');
      renderBoard(host, board, {
        attentionLabels,
        onSelect: () => {},
        announce: () => {},
        onLoadMore: async () => {},
      });
      const scroller = host.querySelector('.board-scroller');
      window.__dishRepeatedRenderScrollCalls = 0;
      scroller.scrollBy = () => { window.__dishRepeatedRenderScrollCalls += 1; };
      host.focus();
      host.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
      return window.__dishRepeatedRenderScrollCalls;
    }""")
    assert calls == 1
    prepare_page(page, "app", "task-biryani")
    assert page.get_by_role("dialog").is_visible()
    page.get_by_role("button", name="Close task detail").click()
    assert page.locator("body").get_attribute("data-prototype-route") == "/"
    prepare_page(page, "loading")
    assert page.locator('[aria-busy="true"]').is_visible()
    page.emulate_media(reduced_motion="reduce")
    assert page.locator(".board-skeleton__column").first.evaluate("node => getComputedStyle(node).animationName") == "none"
    prepare_page(page, "initial-error")
    assert page.get_by_role("button", name="Retry board load").is_visible()
    prepare_page(page, "last-safe")
    assert page.get_by_text("Refresh unavailable").is_visible()
    prepare_page(page, "zero")
    assert page.get_by_text("No active sections").is_visible()
    prepare_page(page, "extreme", "task-extreme", review_mode=True)
    assert page.get_by_role("navigation", name="Fixture review scenarios").is_visible()
    assert page.get_by_role("dialog").is_visible()
    blocked = page.evaluate("fetch('/api/board').then(() => false).catch(error => error.message.includes('blocks backend'))")
    assert blocked
    page.close()



def assert_visual_resilience(browser) -> None:
    for width, height in ((1024, 768), (1280, 800), (1440, 900), (1920, 1080)):
        page = browser.new_page(viewport={"width": width, "height": height})
        prepare_page(page, "extreme", "task-extreme", review_mode=True)
        page.get_by_role("dialog").wait_for()
        metrics = page.evaluate("""() => {
          const panel = document.querySelector('.task-detail').getBoundingClientRect();
          const select = document.querySelector('.review-toolbar select').getBoundingClientRect();
          const notices = document.querySelector('.notice-stack');
          const cards = document.querySelector('.board-column__cards');
          return {
            pageWidth: document.documentElement.scrollWidth,
            pageHeight: document.documentElement.scrollHeight,
            panelLeft: panel.left,
            panelWidth: panel.width,
            selectRight: select.right,
            noticesClient: notices.clientHeight,
            noticesScroll: notices.scrollHeight,
            cardsClient: cards.clientHeight,
            cardsScroll: cards.scrollHeight,
          };
        }""")
        assert metrics["pageWidth"] <= width
        assert metrics["pageHeight"] <= height
        assert 400 <= metrics["panelWidth"] <= 500
        assert metrics["selectRight"] <= metrics["panelLeft"]
        assert metrics["noticesClient"] <= 216
        assert metrics["noticesScroll"] >= metrics["noticesClient"]
        assert metrics["cardsScroll"] >= metrics["cardsClient"]
        page.close()



def assert_local_postgresql(browser) -> None:
    url = os.environ.get("DISH_FRONTEND_LOCAL_URL")
    if not url:
        raise RuntimeError("DISH_FRONTEND_LOCAL_URL is required for local-postgresql mode")
    expected_title = os.environ.get("DISH_FRONTEND_LOCAL_EXPECTED_TITLE", "[ready] Exact imported task")
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(url, wait_until="networkidle")
    page.locator('#app[data-shell-state="local-postgresql-board"]').wait_for()
    assert page.get_by_text("LOCAL POSTGRESQL — NON-AUTHORITATIVE", exact=True).is_visible()
    assert page.get_by_text(expected_title, exact=True).is_visible()
    page.get_by_role("button", name=re.compile(re.escape(expected_title))).click()
    page.locator('#app[data-shell-state="local-postgresql-detail"]').wait_for()
    page.get_by_role("heading", name=expected_title, exact=True).wait_for()
    detail_path = page.locator("body").evaluate("() => location.pathname")
    assert re.match(r"^/dishes/(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[^/]+$", detail_path)
    page.reload(wait_until="networkidle")
    page.get_by_role("heading", name=expected_title, exact=True).wait_for()
    assert page.locator("body").evaluate("() => location.pathname") == detail_path
    content = page.locator("body").inner_text()
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", content, re.I)
    page.get_by_role("button", name="Close task detail").click()
    page.locator('#app[data-shell-state="local-postgresql-board"]').wait_for()
    page.close()

def capture_shells(browser) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    for view, filename in (("login", "stage-0-login.png"), ("app", "stage-1a-board.png"), ("zero", "stage-1a-zero-board.png")):
        prepare_page(page, view)
        page.screenshot(path=SCREENSHOTS / filename, full_page=True)
    prepare_page(page, "app")
    page.get_by_role("button", name=re.compile("Chicken biryani")).click()
    page.screenshot(path=SCREENSHOTS / "stage-1b-task-detail.png", full_page=True)
    prepare_page(page, "app")
    page.get_by_role("button", name=re.compile("Aubergine")).click()
    page.screenshot(path=SCREENSHOTS / "stage-1b-render-fallback.png", full_page=True)
    for view in ("loading", "initial-error", "last-safe"):
        prepare_page(page, view)
        page.screenshot(path=SCREENSHOTS / f"stage-1c-{view}.png", full_page=True)
    prepare_page(page, "app")
    page.screenshot(path=SCREENSHOTS / "stage-1c-grouped-banners.png", full_page=True)
    prepare_page(page, "extreme", "task-extreme", review_mode=True)
    page.screenshot(path=SCREENSHOTS / "stage-1e-review-extreme.png", full_page=True)
    page.screenshot(path=SCREENSHOTS / "stage-1f-polished-extreme.png", full_page=True)
    page.set_viewport_size({"width": 1024, "height": 768})
    prepare_page(page, "app", "task-biryani")
    page.screenshot(path=SCREENSHOTS / "stage-1d-minimum-viewport.png", full_page=True)
    prepare_page(page, "extreme", "task-extreme", review_mode=True)
    page.screenshot(path=SCREENSHOTS / "stage-1f-minimum-viewport.png", full_page=True)
    page.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("test", "screenshots", "local-postgresql"))
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=browser_executable(),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            if args.mode == "test":
                assert_shells(browser)
                assert_visual_resilience(browser)
                assert_stage5_repeated_invalid_cursors(browser, SRC)
                print("Playwright shell, visual-resilience, and Stage 5 cursor checks passed")
            elif args.mode == "local-postgresql":
                assert_local_postgresql(browser)
                print("Local PostgreSQL board/detail browser smoke passed")
            else:
                capture_shells(browser)
                print(f"Captured frontend screenshots in {SCREENSHOTS}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
