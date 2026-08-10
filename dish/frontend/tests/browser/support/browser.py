from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import expect

from .bridge import ORIGIN, attach_network_audit
from .runtime import PASSWORD


class AcceptancePage:
    def __init__(self, page, bridge, artifacts: Path) -> None:
        self.page = page
        self.bridge = bridge
        self.runtime = bridge.runtime
        self.audit = attach_network_audit(page)
        self.artifacts = artifacts
        self.screenshots: list[str] = []

    def goto(self, path: str = "/") -> None:
        self.page.goto(f"{ORIGIN}{path}", wait_until="domcontentloaded")

    def login(self, *, return_path: str = "/", password: str = PASSWORD) -> None:
        self.goto(return_path)
        expect(self.page).to_have_url(re.compile(r"/login\?return="))
        self.page.get_by_label("Shared password").fill(password)
        self.page.get_by_role("button", name="Sign in").click()
        expect(self.page).to_have_url(re.compile(re.escape(ORIGIN) + re.escape(return_path.rstrip("/") or "/")))
        self.page.locator("#app:not([hidden])").wait_for()
        self.audit.responses[:] = [item for item in self.audit.responses if not (item[0] == 401 and "/frontend/session" in item[1])]

    def wait_board(self) -> None:
        self.page.locator('#app[data-shell-state="local-postgresql-board"]').wait_for()

    def wait_detail(self) -> None:
        self.page.locator('#app[data-shell-state="local-postgresql-detail"]').wait_for()
        self.page.get_by_role("dialog").wait_for()

    def screenshot(self, name: str) -> None:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        target = self.artifacts / f"{name}.png"
        self.page.screenshot(path=target, full_page=True)
        self.screenshots.append(str(target))

    def observation(self) -> dict:
        return {
            "console_errors": list(self.audit.console_errors),
            "page_errors": list(self.audit.page_errors),
            "request_failures": list(self.audit.request_failures),
            "http_errors": [list(item) for item in self.audit.unexpected_http_errors()],
            "redirects": [list(item) for item in self.audit.redirects],
            "screenshots": list(self.screenshots),
        }

    def assert_clean(self, *, allowed_http_errors=()) -> None:
        assert self.audit.console_errors == []
        assert self.audit.page_errors == []
        assert self.audit.request_failures == []
        assert self.audit.unexpected_http_errors(allowed_http_errors) == []
