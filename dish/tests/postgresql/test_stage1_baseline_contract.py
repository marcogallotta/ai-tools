from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from dish_service.command_spec import ACTION_COMMANDS
from dish_tool.admin_command_spec import ADMIN_COMMANDS

ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "docs/database-backend-stage-a-baseline.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_frozen_command_inventory_matches_current_surfaces() -> None:
    baseline = _baseline()

    assert list(ACTION_COMMANDS) == baseline["action_commands"]
    assert sorted(ADMIN_COMMANDS) == sorted(baseline["admin_commands"])

    expected_treatments = set(baseline["action_commands"]) | set(baseline["admin_commands"])
    expected_treatments.add("planning-intent-settlement")
    treatments = baseline["target_treatments"]
    source_only = baseline["source_only_commands"]
    assert isinstance(treatments, dict)
    assert isinstance(source_only, list)
    assert not (set(treatments) & set(source_only))
    assert set(treatments) | set(source_only) == expected_treatments
    assert all(
        re.fullmatch(r"(?:retain|retire|add):[A-Z]", treatment)
        for treatment in treatments.values()
    )


_SQLITE_TABLE_STATEMENT = re.compile(
    r"CREATE TABLE(?: IF NOT EXISTS)?\s+(?P<create>[A-Za-z0-9_]+)"
    r"|ALTER TABLE\s+(?P<rename_old>[A-Za-z0-9_]+)\s+RENAME TO\s+(?P<rename_new>[A-Za-z0-9_]+)"
    r"|DROP TABLE(?: IF EXISTS)?\s+(?P<drop>[A-Za-z0-9_]+)"
)


def _sqlite_tables(source: str) -> list[str]:
    """Walk CREATE/RENAME/DROP TABLE statements in source order, keeping each
    table at the list slot of its first CREATE even when a migration renames
    it away, recreates it under the original name, and drops the stale copy.
    """
    slots: list[str | None] = []
    original_name_of_slot: dict[int, str] = {}

    def _slot_holding(name: str) -> int | None:
        return next((index for index, held in enumerate(slots) if held == name), None)

    for match in _SQLITE_TABLE_STATEMENT.finditer(source):
        if match.group("create"):
            name = match.group("create")
            if _slot_holding(name) is not None:
                continue
            restored_slot = next(
                (index for index, original in original_name_of_slot.items() if original == name),
                None,
            )
            if restored_slot is not None:
                slots[restored_slot] = name
            else:
                original_name_of_slot[len(slots)] = name
                slots.append(name)
        elif match.group("rename_old"):
            old_name, new_name = match.group("rename_old"), match.group("rename_new")
            slot = _slot_holding(old_name)
            if slot is not None:
                slots[slot] = new_name
        elif match.group("drop"):
            name = match.group("drop")
            slot = _slot_holding(name)
            if slot is not None:
                slots[slot] = None
    return [name for name in slots if name is not None]


def test_frozen_sqlite_authority_inventory_matches_schema() -> None:
    baseline = _baseline()
    source = (ROOT / "dish_tool/database_schema.py").read_text(encoding="utf-8")
    assert _sqlite_tables(source) == baseline["sqlite_tables"]


def test_frozen_governing_sources_have_exact_hashes() -> None:
    baseline = _baseline()
    actual = {
        relative: _sha256(ROOT / relative)
        for relative in baseline["governing_source_sha256"]
    }
    assert actual == baseline["governing_source_sha256"]


def test_frozen_characterization_corpus_has_exact_hashes() -> None:
    baseline = _baseline()
    actual = {
        str(path.relative_to(ROOT)): _sha256(path)
        for path in sorted((ROOT / "tests").glob("test_*.py"))
    }
    assert actual == baseline["characterization_test_sha256"]


def test_canonical_stage_a_regeneration_matches_checked_in_bytes(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = ROOT / "scripts/dish-pg-stage-a-baseline"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for output in (first, second):
        completed = subprocess.run(
            [sys.executable, str(script), "--output", str(output)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout
    assert first.read_bytes() == second.read_bytes() == BASELINE_PATH.read_bytes()

    checked = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout
    assert "matches canonical regeneration" in checked.stdout


def test_governed_stage_a_write_requires_a_reason(tmp_path: Path) -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "dish-pg-stage-a-baseline"),
            "--write",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 2
    assert "--reason is required" in completed.stdout
