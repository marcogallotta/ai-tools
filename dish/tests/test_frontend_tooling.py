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
    result = run_npm("check")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.smoke
def test_production_frontend_build_excludes_fixture_review_assets() -> None:
    dist = FRONTEND / "dist"
    metadata = json.loads((dist / "build.json").read_text(encoding="utf-8"))
    assert metadata["fixtureBacked"] is False
    assert metadata["networkMode"] == "read-only-postgresql"
    for relative in (
        "fixtures",
        "js/prototype",
        "js/review",
        "styles/review.css",
    ):
        assert not (dist / relative).exists(), relative
    production_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in dist.rglob("*.js")
    )
    assert "Fixture prototype" not in production_text
    assert 'TASK_ROUTE_PREFIX = "/task/"' not in production_text


@pytest.mark.boundary
def test_frontend_chromium_shell_harness() -> None:
    if shutil.which("chromium") is None:
        pytest.skip("Chromium is not installed")
    result = run_npm("test:browser")
    assert result.returncode == 0, result.stdout + result.stderr
