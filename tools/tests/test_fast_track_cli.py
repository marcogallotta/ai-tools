from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "fast-track"
GIT_COMMIT = ROOT / "tools" / "git-commit"


def run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    run(tmp_path, "git", "init", "--bare", str(remote))
    run(tmp_path, "git", "clone", str(remote), str(work))
    run(work, "git", "config", "user.name", "Fast Track Test")
    run(work, "git", "config", "user.email", "fast-track@example.test")
    run(work, "git", "switch", "-c", "main")
    (work / "tools").mkdir()
    shutil.copy2(GIT_COMMIT, work / "tools" / "git-commit")
    (work / "sample.txt").write_text("before\n", encoding="utf-8")
    run(work, "git", "add", "tools/git-commit", "sample.txt")
    run(work, "git", "commit", "-m", "initial")
    run(work, "git", "push", "-u", "origin", "main")
    return work, remote


def test_main_commits_pushes_and_reads_back(repository: tuple[Path, Path]):
    work, remote = repository
    (work / "sample.txt").write_text("after\n", encoding="utf-8")
    result = run(
        work, str(SCRIPT), "main", "--words", "right to main NOW", "-C", str(work),
        "-m", "fast correction", "--", "sample.txt",
    )
    payload = json.loads(result.stdout)
    assert payload["route"] == "main"
    assert payload["published_head"] == payload["readback"]
    assert run(remote, "git", "show", "refs/heads/main:sample.txt").stdout == "after\n"


def test_pr_commits_and_nonforce_publishes_isolated_branch(repository: tuple[Path, Path]):
    work, remote = repository
    run(work, "git", "switch", "-c", "agent/urgent")
    (work / "sample.txt").write_text("candidate\n", encoding="utf-8")
    result = run(
        work, str(SCRIPT), "pr", "--words", "fastrack to PR", "-C", str(work),
        "-m", "urgent candidate", "--", "sample.txt",
    )
    payload = json.loads(result.stdout)
    assert payload["route"] == "pr"
    assert payload["published_head"] in payload["grant_marker"]
    assert run(remote, "git", "show", "refs/heads/agent/urgent:sample.txt").stdout == "candidate\n"


def test_testing_round_trip_restores_then_reapplies_exact_bytes(repository: tuple[Path, Path], tmp_path: Path):
    work, _remote = repository
    bundle = tmp_path / "tested.json"
    run(work, str(SCRIPT), "testing-start", "--words", "fastrack to testing", "-C", str(work), "--", "sample.txt")
    (work / "sample.txt").write_bytes(b"tested\x00bytes\n")
    run(work, str(SCRIPT), "testing-finish", "-C", str(work), "--output", str(bundle))
    assert (work / "sample.txt").read_bytes() == b"before\n"
    run(work, "git", "switch", "-c", "agent/tested")
    run(work, str(SCRIPT), "testing-apply", "-C", str(work), "--bundle", str(bundle))
    assert (work / "sample.txt").read_bytes() == b"tested\x00bytes\n"
