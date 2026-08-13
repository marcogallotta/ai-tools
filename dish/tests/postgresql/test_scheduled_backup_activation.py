from __future__ import annotations

import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"
DEFAULT_LOCAL_ROOT = "/home/marco/.local/state/dish/prod/postgresql-backups"


def test_first_activation_prepares_missing_default_local_root_before_backup() -> None:
    service = (SYSTEMD / "dish-postgres-backup.service").read_text(encoding="utf-8")

    prepare = (
        "ExecStartPre=+/usr/bin/install -d -m 0700 -o marco -g marco "
        + DEFAULT_LOCAL_ROOT
    )
    start = (
        "ExecStart=/home/marco/ai-tools/dish/.venv/bin/python "
        "scripts/dish-pg-scheduled-backup run"
    )

    assert prepare in service
    assert f"ReadWritePaths=-{DEFAULT_LOCAL_ROOT}" in service
    assert "ProtectHome=read-only" in service
    assert service.index(prepare) < service.index(start)


def test_prestart_directory_creation_handles_missing_parents_with_mode_0700(
    tmp_path: Path,
) -> None:
    target = tmp_path / "missing" / "parents" / "postgresql-backups"
    assert not target.exists()

    subprocess.run(
        ["/usr/bin/install", "-d", "-m", "0700", str(target)],
        check=True,
        text=True,
    )

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_custom_local_root_must_be_precreated_and_allowlisted() -> None:
    env = (SYSTEMD / "postgres-backup.env.example").read_text(encoding="utf-8")

    assert "pre-create the custom directory with equivalent ownership/mode" in env
    assert "add it to a ReadWritePaths= service drop-in before activation" in env
