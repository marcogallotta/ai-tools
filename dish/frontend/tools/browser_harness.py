from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright


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
)
IMPORT_RE = re.compile(r'^import\s+.*?\s+from\s+["\'](.+?)["\'];?\s*$', re.MULTILINE)


def browser_executable() -> str:
    configured = os.environ.get("CHROMIUM_BIN")
    executable = configured or shutil.which("chromium")
    if not executable:
        raise RuntimeError("Chromium executable is required")
    return executable


def module_bundle(entry: Path) -> str:
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

    visit(entry)
    return "\n".join(ordered)


def prepare_page(page, view: str) -> None:
    page.set_content('<main id="app" class="app-root" aria-live="polite"></main>')
    css = "\n".join((SRC / "styles" / name).read_text(encoding="utf-8") for name in STYLE_FILES)
    page.add_style_tag(content=css)
    bundle = module_bundle(SRC / "js" / "boot.js")
    page.add_script_tag(content=bundle)
    if view == "login":
        page.evaluate("renderLoginShell(document.querySelector('#app'))")
    elif view == "zero":
        page.evaluate("renderFixturePrototype(document.querySelector('#app'), 'zero')")
    else:
        page.evaluate("renderFixturePrototype(document.querySelector('#app'), 'board')")


def assert_shells(browser) -> None:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    prepare_page(page, "login")
    page.locator('#app[data-shell-state="login"]').wait_for()
    assert page.get_by_label("Shared password").is_disabled()
    prepare_page(page, "app")
    page.locator('#app[data-shell-state="fixture-board"]').wait_for()
    assert page.locator('[aria-label="Dish task board"]').is_visible()
    assert page.get_by_role("button", name="Load more").is_visible()
    prepare_page(page, "zero")
    assert page.get_by_text("No active sections").is_visible()
    page.close()


def capture_shells(browser) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    for view, filename in (("login", "stage-0-login.png"), ("app", "stage-1a-board.png"), ("zero", "stage-1a-zero-board.png")):
        prepare_page(page, view)
        page.screenshot(path=SCREENSHOTS / filename, full_page=True)
    page.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("test", "screenshots"))
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
                print("Playwright shell checks passed")
            else:
                capture_shells(browser)
                print(f"Captured frontend screenshots in {SCREENSHOTS}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
