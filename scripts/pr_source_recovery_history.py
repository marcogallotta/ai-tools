from __future__ import annotations

from pathlib import Path
from typing import Iterable

from pr_source_recovery_types import RecoveryPlan, git


def parents(repo: Path, commit_sha: str) -> tuple[str, ...]:
    line = git(repo, "show", "-s", "--format=%P", commit_sha).stdout.strip()
    return tuple(part for part in line.split() if part)


def first_parent_chain(repo: Path, commit_sha: str) -> set[str]:
    return {
        line.strip()
        for line in git(repo, "rev-list", "--first-parent", commit_sha).stdout.splitlines()
        if line.strip()
    }


def paths_between(repo: Path, before: str, after: str) -> tuple[str, ...]:
    output = git(repo, "diff", "--name-only", before, after).stdout
    return tuple(sorted({line.strip() for line in output.splitlines() if line.strip()}))


def later_touching_paths(
    repo: Path,
    *,
    landed_sha: str,
    current_main_sha: str,
    landed_paths: Iterable[str],
) -> tuple[str, ...]:
    touched: set[str] = set()
    for path in landed_paths:
        result = git(repo, "log", "--format=%H", f"{landed_sha}..{current_main_sha}", "--", path)
        if result.stdout.strip():
            touched.add(path)
    return tuple(sorted(touched))


def failure_plan(
    *,
    repo: Path,
    landed_sha: str,
    current_main_sha: str,
    current_main_ref: str,
    commit_parents: tuple[str, ...],
    landing_kind: str | None,
    mainline_parent: str | None,
    reason: str,
    known_residual_effects: tuple[str, ...],
    changed_paths: tuple[str, ...] = (),
    later_touching_paths: tuple[str, ...] = (),
    conflict_paths: tuple[str, ...] = (),
) -> RecoveryPlan:
    return RecoveryPlan(
        schema="dish-source-recovery-plan-v1",
        status="semantic_implementation_required",
        repository_path=str(repo),
        landed_sha=landed_sha,
        current_main_sha=current_main_sha,
        current_main_ref=current_main_ref,
        landing_kind=landing_kind,
        landed_parents=commit_parents,
        mainline_parent=mainline_parent,
        changed_paths=changed_paths,
        later_touching_paths=later_touching_paths,
        conflict_paths=conflict_paths,
        inverse_tree_sha=None,
        reason=reason,
        source_reversal_scope="git-source-only",
        runtime_effects_reversed=False,
        known_residual_effects=known_residual_effects,
        next_action=(
            "return to semantic Implementation on a new recovery branch from the exact current main; "
            "do not improvise the inverse in Integration"
        ),
    )
