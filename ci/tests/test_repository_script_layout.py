from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLANNER_PATH = ROOT / "scripts" / "integration_certification_plan.py"
SPEC = importlib.util.spec_from_file_location(
    "integration_certification_plan_layout_contract", PLANNER_PATH
)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)

MECHANICAL_DELEGATION_SHIM_V1 = b'''#!/usr/bin/env python3
# DISH_MECHANICAL_DELEGATION_SHIM_V1
from __future__ import annotations

import os
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "dish" / "scripts" / Path(__file__).name

if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(TARGET), *sys.argv[1:]])
'''


def _is_executable(path: Path) -> bool:
    return path.is_file() and bool(path.stat().st_mode & 0o111)


def _duplicate_executable_names(repo_root: Path) -> tuple[str, ...]:
    root_names = {
        path.name for path in (repo_root / "scripts").iterdir() if _is_executable(path)
    }
    dish_names = {
        path.name for path in (repo_root / "dish" / "scripts").iterdir() if _is_executable(path)
    }
    return tuple(sorted(root_names & dish_names))


def _divergent_duplicate_names(repo_root: Path) -> tuple[str, ...]:
    divergent: list[str] = []
    for name in _duplicate_executable_names(repo_root):
        root_path = repo_root / "scripts" / name
        if root_path.read_bytes() != MECHANICAL_DELEGATION_SHIM_V1:
            divergent.append(name)
    return tuple(divergent)


def _write_executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def test_repository_has_no_divergent_duplicate_executables() -> None:
    assert _divergent_duplicate_names(ROOT) == ()


def test_contract_rejects_a_new_divergent_duplicate(tmp_path: Path) -> None:
    _write_executable(tmp_path / "scripts" / "example-runner", b"#!/bin/sh\nexit 0\n")
    _write_executable(tmp_path / "dish" / "scripts" / "example-runner", b"#!/bin/sh\nexit 0\n")

    assert _divergent_duplicate_names(tmp_path) == ("example-runner",)


def test_contract_allows_only_the_explicit_mechanical_delegation_shim(tmp_path: Path) -> None:
    _write_executable(
        tmp_path / "scripts" / "example-runner",
        MECHANICAL_DELEGATION_SHIM_V1,
    )
    _write_executable(tmp_path / "dish" / "scripts" / "example-runner", b"#!/bin/sh\nexit 0\n")

    assert _divergent_duplicate_names(tmp_path) == ()


def test_cleanup_paths_have_narrow_repository_planner_owners() -> None:
    plan = planner.build_repository_plan(
        [
            "scripts/dish-pg-pglite",
            "ci/tests/test_repository_script_layout.py",
        ],
        candidate_sha="a" * 40,
        base_sha="b" * 40,
        merge_base_sha="c" * 40,
        semantic_review_complete=True,
    )

    assert plan["classifications"] == [
        {
            "path": "ci/tests/test_repository_script_layout.py",
            "scope": "repository",
            "classification": "ci-control-plane",
        },
        {
            "path": "scripts/dish-pg-pglite",
            "scope": "repository",
            "classification": "root-scripts",
        },
    ]
    assert plan["force_full"] is False
    assert plan["selected_lanes"] == ["repository control-plane"]
    assert plan["selected_groups"] == ["python-control-plane"]
