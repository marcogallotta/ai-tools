from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_dish_service_help_uses_the_repository_virtualenv_without_starting_service():
    completed = subprocess.run(
        [str(ROOT / "dish-service"), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("usage: dish-service")
    assert "Run the single-process Dish HTTP service" in completed.stdout
    assert completed.stderr == ""


def test_dish_service_fails_closed_when_repository_virtualenv_is_missing(tmp_path):
    launcher = tmp_path / "dish-service"
    launcher.write_text((ROOT / "dish-service").read_text())
    launcher.chmod(0o755)
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)

    completed = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    expected = tmp_path / ".venv" / "bin" / "python"
    assert f"dish-service: no virtualenv at {expected}" in completed.stderr
