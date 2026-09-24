from __future__ import annotations

import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"
DEFAULT_LOCAL_ROOT = "/home/marco/.local/state/dish/prod/postgresql-backups"


def test_first_activation_prepares_missing_default_local_root_before_backup() -> None:
    service = (SYSTEMD / "dish-postgres-backup.service").read_text(encoding="utf-8")

    # The user-manager unit no longer bootstraps the default writable root with a
    # privileged ExecStartPre install; systemd's own StateDirectory= creates it
    # (mode 0700, owned by the running user-manager user) before ExecStart runs.
    relative_state_directory = DEFAULT_LOCAL_ROOT.removeprefix(
        "/home/marco/.local/state/"
    )
    assert f"StateDirectory={relative_state_directory}" in service
    assert "StateDirectoryMode=0700" in service
    assert "ProtectHome=read-only" in service


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
