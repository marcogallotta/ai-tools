from __future__ import annotations

import sqlite3

import pytest

from dish_service.path_safety import (
    KillSwitchPathError,
    clear_kill_switch,
    engage_kill_switch,
)


def test_kill_switch_operations_never_replace_or_unlink_unrelated_file(tmp_path):
    database = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE authority(id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    original = database.read_bytes()

    with pytest.raises(KillSwitchPathError):
        engage_kill_switch(database, {"disabled": True, "reason": "operator typo"})
    with pytest.raises(KillSwitchPathError):
        clear_kill_switch(database)

    assert database.read_bytes() == original


def test_kill_switch_create_replay_and_clear_are_marker_specific(tmp_path):
    marker = tmp_path / "dark-launch.disabled"
    payload = {"disabled": True, "reason": "test"}

    assert engage_kill_switch(marker, payload) is True
    assert engage_kill_switch(marker, payload) is False
    assert clear_kill_switch(marker) is True
    assert clear_kill_switch(marker) is False
