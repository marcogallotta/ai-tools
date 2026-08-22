from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from test_selection.validator import _scoped_paths, validate_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "test_selection" / "ownership.csv"


def _rows() -> tuple[list[str], list[dict[str, str]]]:
    with POLICY.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _write_policy(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_current_head_map_is_structurally_valid() -> None:
    result = validate_policy(repo_root=ROOT, parent_root=ROOT.parent, policy_path=POLICY)

    assert result.errors == ()
    assert result.row_count == result.expected_repo_paths + 4




def test_ignored_generated_state_does_not_change_tracked_selection_authority(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "frontend" / "src" / "app.js"
    generated = tmp_path / "frontend" / "dist" / "app.js"
    source.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("frontend/dist/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore", "frontend/src/app.js"], check=True)

    without_generated = _scoped_paths(tmp_path)
    generated.write_text("generated", encoding="utf-8")
    with_generated = _scoped_paths(tmp_path)

    assert without_generated == with_generated
    assert "frontend/src/app.js" in with_generated
    assert "frontend/dist/app.js" not in with_generated


def test_missing_current_path_is_rejected(tmp_path: Path) -> None:
    fields, rows = _rows()
    rows = [row for row in rows if row["path"] != "dish_tool/review_queue.py"]
    changed = tmp_path / "missing.csv"
    _write_policy(changed, fields, rows)

    result = validate_policy(repo_root=ROOT, parent_root=ROOT.parent, policy_path=changed)

    assert any("unclassified in-scope repository paths" in error for error in result.errors)


def test_duplicate_path_is_rejected(tmp_path: Path) -> None:
    fields, rows = _rows()
    rows.append(dict(rows[0]))
    changed = tmp_path / "duplicate.csv"
    _write_policy(changed, fields, rows)

    result = validate_policy(repo_root=ROOT, parent_root=ROOT.parent, policy_path=changed)

    assert any("duplicate mapped paths" in error for error in result.errors)


def test_missing_owner_test_is_rejected(tmp_path: Path) -> None:
    fields, rows = _rows()
    target = next(row for row in rows if row["path"] == "dish_tool/review_queue.py")
    target["direct_owner_tests"] = "tests/test_does_not_exist.py"
    changed = tmp_path / "bad-owner.csv"
    _write_policy(changed, fields, rows)

    result = validate_policy(repo_root=ROOT, parent_root=ROOT.parent, policy_path=changed)

    assert any("references missing file" in error for error in result.errors)
