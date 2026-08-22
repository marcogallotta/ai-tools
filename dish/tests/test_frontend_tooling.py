from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def run_npm(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npm", "run", script],
        cwd=FRONTEND,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.smoke
def test_frontend_tooling_and_unit_contract() -> None:
    scripts = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["check"] == "npm run check:static && npm run test:acceptance:built"
    assert scripts["check:static"] == (
        "npm run format:check && npm run lint && npm run schema:check "
        "&& npm run test:unit && npm run build"
    )
    assert scripts["test:acceptance"] == "npm run build && npm run test:acceptance:built"
    assert scripts["test:browser"] == "../.venv/bin/python tools/browser_harness.py test"


@pytest.mark.boundary
def test_frontend_chromium_shell_harness() -> None:
    if shutil.which("chromium") is None:
        pytest.skip("Chromium is not installed")
    result = run_npm("test:browser")
    assert result.returncode == 0, result.stdout + result.stderr
