from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dish_pg import location_manifest as manifest_module
from dish_pg.location_manifest import LocationManifestError, _atomic_json, _environment_file


def _write_env(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def test_environment_file_rejects_insecure_symlink_duplicate_and_malformed_input(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "test.env"
    _write_env(env_file, "DISH_COOKING_PROJECT_GID=900\n", mode=0o644)
    with pytest.raises(LocationManifestError, match="mode 0600"):
        _environment_file(env_file)

    env_file.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(env_file)
    with pytest.raises(LocationManifestError, match="must not use symlinks"):
        _environment_file(link, reject_symlinks=True)

    _write_env(env_file, "A=one\nA=two\n")
    with pytest.raises(LocationManifestError, match="duplicate"):
        _environment_file(env_file, reject_duplicates=True)
    _write_env(env_file, "not an assignment\n")
    with pytest.raises(LocationManifestError, match="invalid environment assignment"):
        _environment_file(env_file)


def test_atomic_output_rejects_symlinks_aliases_and_replaces_owner_only(
    tmp_path: Path, monkeypatch
) -> None:
    protected = tmp_path / "protected"
    protected.write_text("source", encoding="utf-8")
    protected.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(protected)
    with pytest.raises(LocationManifestError, match="must not use symlinks"):
        _atomic_json(link, {"tasks": {}}, protected_paths=(protected,))

    alias = tmp_path / "alias.json"
    os.link(protected, alias)
    with pytest.raises(LocationManifestError, match="must not alias"):
        _atomic_json(alias, {"tasks": {}}, protected_paths=(protected,))

    destination = tmp_path / "manifest.json"
    destination.write_text("old", encoding="utf-8")
    observed = []
    original_replace = os.replace

    def replace(source, target):
        observed.append((destination.read_text(encoding="utf-8"), Path(source).stat().st_mode & 0o777))
        original_replace(source, target)

    monkeypatch.setattr(manifest_module.os, "replace", replace)
    _atomic_json(destination, {"tasks": {}})
    assert observed == [("old", 0o600)]
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(destination.read_text(encoding="utf-8")) == {"tasks": {}}


def test_atomic_output_rejects_concurrent_destination_change(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "manifest.json"
    destination.write_text("old", encoding="utf-8")
    original_dump = manifest_module.json.dump

    def dump(value, handle, **kwargs):
        destination.write_text("concurrent", encoding="utf-8")
        return original_dump(value, handle, **kwargs)

    monkeypatch.setattr(manifest_module.json, "dump", dump)
    with pytest.raises(LocationManifestError, match="changed during atomic replacement"):
        _atomic_json(destination, {"tasks": {}}, protected_paths=(tmp_path / "protected",))
    assert destination.read_text(encoding="utf-8") == "concurrent"
    assert not list(tmp_path.glob(".manifest.json.*"))
