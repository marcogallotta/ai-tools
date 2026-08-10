from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

DISH_ROOT = Path(__file__).resolve().parents[3]
if str(DISH_ROOT) not in sys.path:
    sys.path.insert(0, str(DISH_ROOT))

from frontend.tests.browser.support import AcceptancePage, ProductionBridge  # noqa: E402

ARTIFACT_ROOT = DISH_ROOT / ".test-artifacts" / "frontend-stage7"
_RESULTS: list[dict[str, str]] = []
_OBSERVATIONS: list[dict] = []


def _chromium() -> str:
    configured = os.environ.get("CHROMIUM_BIN")
    executable = configured or shutil.which("chromium")
    if not executable:
        raise RuntimeError("Chromium executable is required for Stage 7 browser acceptance")
    return executable


@pytest.fixture(scope="session")
def browser_engine():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=_chromium(),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def acceptance(browser_engine, tmp_path: Path, request):
    static_root = DISH_ROOT / "frontend" / "dist"
    assert (static_root / "build.json").is_file(), "run npm --prefix frontend run build first"
    bridge = ProductionBridge(static_root=static_root, scratch_root=tmp_path)
    context = browser_engine.new_context(viewport={"width": 1440, "height": 900})
    bridge.install(context)
    page = context.new_page()
    wrapper = AcceptancePage(page, bridge, ARTIFACT_ROOT / request.node.name)
    try:
        yield wrapper
    finally:
        _OBSERVATIONS.append({"scenario": request.node.nodeid, **wrapper.observation()})
        context.close()
        bridge.close()


def pytest_runtest_logreport(report):
    if report.when == "call":
        _RESULTS.append({"scenario": report.nodeid, "result": "passed" if report.passed else "failed" if report.failed else "skipped"})


def pytest_sessionfinish(session, exitstatus):
    del session
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    build = DISH_ROOT / "frontend" / "dist" / "build.json"
    payload = {
        "schema": "dish-frontend-stage7-run-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "exit_status": exitstatus,
        "chromium": _chromium(),
        "origin": "https://dish.example.test",
        "viewport": {"width": 1440, "height": 900},
        "refresh_interval_seconds": 1,
        "build": json.loads(build.read_text(encoding="utf-8")) if build.is_file() else None,
        "scenarios": _RESULTS,
        "observations": _OBSERVATIONS,
    }
    (ARTIFACT_ROOT / "run.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
