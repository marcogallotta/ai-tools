from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pr_source_recovery as recovery


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def write(repo: Path, path: str, value: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Dish Test")
    git(path, "config", "user.email", "dish@example.invalid")
    write(path, "base.txt", "base\n")
    commit(path, "base")
    return path


def set_origin_main(path: Path, sha: str) -> None:
    git(path, "update-ref", "refs/remotes/origin/main", sha)


def test_one_parent_inverse_preserves_later_unrelated_work(tmp_path):
    path = repo(tmp_path)
    write(path, "bad.txt", "unsafe\n")
    landed = commit(path, "squash landing")
    write(path, "later.txt", "keep me\n")
    current = commit(path, "later unrelated")
    set_origin_main(path, current)

    plan = recovery.build_plan(repo=path, landed_sha=landed, current_main_sha=current)

    assert plan.status == "candidate"
    assert plan.landing_kind == "one-parent"
    assert plan.mainline_parent
    assert plan.changed_paths == ("bad.txt",)
    assert plan.runtime_effects_reversed is False

    recovery.apply_plan(
        repo=path,
        landed_sha=landed,
        current_main_sha=current,
        expected_tree_sha=plan.inverse_tree_sha,
    )
    assert not (path / "bad.txt").exists()
    assert (path / "later.txt").read_text() == "keep me\n"


def test_true_merge_uses_verified_first_parent_as_mainline(tmp_path):
    path = repo(tmp_path)
    base = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-b", "feature")
    write(path, "feature.txt", "feature\n")
    commit(path, "feature")
    git(path, "checkout", "main")
    write(path, "main.txt", "main work\n")
    main_parent = commit(path, "main work")
    git(path, "merge", "--no-ff", "feature", "-m", "true merge")
    landed = git(path, "rev-parse", "HEAD")
    write(path, "later.txt", "later\n")
    current = commit(path, "later")
    set_origin_main(path, current)

    plan = recovery.build_plan(repo=path, landed_sha=landed, current_main_sha=current)

    assert base != main_parent
    assert plan.status == "candidate"
    assert plan.landing_kind == "true-merge"
    assert plan.mainline_parent == main_parent
    recovery.apply_plan(repo=path, landed_sha=landed, current_main_sha=current)
    assert not (path / "feature.txt").exists()
    assert (path / "main.txt").read_text() == "main work\n"
    assert (path / "later.txt").read_text() == "later\n"


def test_conflicting_later_work_fails_to_semantic_implementation(tmp_path):
    path = repo(tmp_path)
    write(path, "value.txt", "bad\n")
    landed = commit(path, "bad landing")
    write(path, "value.txt", "later depends on bad\n")
    current = commit(path, "dependent later change")
    set_origin_main(path, current)

    plan = recovery.build_plan(repo=path, landed_sha=landed, current_main_sha=current)

    assert plan.status == "semantic_implementation_required"
    assert plan.conflict_paths == ("value.txt",)
    assert "conflict" in (plan.reason or "")


def test_non_first_parent_landed_identity_is_ambiguous(tmp_path):
    path = repo(tmp_path)
    git(path, "checkout", "-b", "feature")
    write(path, "feature.txt", "feature\n")
    feature_tip = commit(path, "feature")
    git(path, "checkout", "main")
    git(path, "merge", "--no-ff", "feature", "-m", "merge")
    current = git(path, "rev-parse", "HEAD")
    set_origin_main(path, current)

    plan = recovery.build_plan(repo=path, landed_sha=feature_tip, current_main_sha=current)

    assert plan.status == "semantic_implementation_required"
    assert "first-parent" in (plan.reason or "")


def test_main_movement_after_plan_is_rejected(tmp_path):
    path = repo(tmp_path)
    write(path, "bad.txt", "bad\n")
    landed = commit(path, "bad landing")
    current = landed
    set_origin_main(path, current)
    plan = recovery.build_plan(repo=path, landed_sha=landed, current_main_sha=current)
    assert plan.status == "candidate"

    write(path, "moved.txt", "new main\n")
    moved = commit(path, "main moved")
    set_origin_main(path, moved)
    git(path, "reset", "--hard", current)

    with pytest.raises(recovery.RecoveryError, match="current-main movement detected"):
        recovery.apply_plan(
            repo=path,
            landed_sha=landed,
            current_main_sha=current,
            expected_tree_sha=plan.inverse_tree_sha,
        )


def test_known_runtime_effects_remain_explicit_residual_gates(tmp_path):
    path = repo(tmp_path)
    write(path, "migration.sql", "alter table example add column unsafe int;\n")
    landed = commit(path, "bad migration source")
    set_origin_main(path, landed)

    plan = recovery.build_plan(
        repo=path,
        landed_sha=landed,
        current_main_sha=landed,
        known_residual_effects=["database", "deployment"],
    )

    assert plan.status == "candidate"
    assert plan.source_reversal_scope == "git-source-only"
    assert plan.runtime_effects_reversed is False
    assert plan.known_residual_effects == ("database", "deployment")
