from __future__ import annotations

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
    result = run_npm("check")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.boundary
def test_frontend_chromium_shell_harness() -> None:
    if shutil.which("chromium") is None:
        pytest.skip("Chromium is not installed")
    result = run_npm("test:browser")
    assert result.returncode == 0, result.stdout + result.stderr
