from __future__ import annotations

import os
import stat
from pathlib import Path

from dish_service.backup import BackupManager
from dish_tool.database_schema import initialize_database


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_successful_restore_installs_live_database_owner_only(tmp_path):
    live = tmp_path / "dish.db"
    backups = tmp_path / "backups"
    initialize_database(live).close()
    os.chmod(live, 0o600)

    manager = BackupManager(live, backups)
    backup = manager.create(label="mode")
    assert _mode(live) == 0o600

    manager.restore(backup.backup_id)

    assert _mode(live) == 0o600


def test_recovery_reasserts_owner_only_mode_after_committed_replacement(tmp_path):
    live = tmp_path / "dish.db"
    backups = tmp_path / "backups"
    initialize_database(live).close()
    manager = BackupManager(live, backups)
    backup = manager.create(label="mode-recovery")

    plan = manager._prepare_restore(backup.backup_id)
    candidate = Path(plan["candidate"]["path"])
    os.replace(candidate, live)
    os.chmod(live, 0o644)
    assert _mode(live) == 0o644

    restored = manager.recover_restore(
        backup.backup_id,
        {"stage": "replacement_committed", "details": {"plan": plan}},
    )

    assert restored["restored"]["source_backup_id"] == backup.backup_id
    assert _mode(live) == 0o600
