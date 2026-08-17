from __future__ import annotations

from pathlib import Path
from typing import Iterable

from pr_source_recovery_planner import build_plan
from pr_source_recovery_types import RecoveryError, RecoveryPlan, git, inverse_args, sha


def apply_plan(
    *,
    repo: Path,
    landed_sha: str,
    current_main_sha: str,
    current_main_ref: str = "refs/remotes/origin/main",
    expected_tree_sha: str | None = None,
    known_residual_effects: Iterable[str] = (),
) -> RecoveryPlan:
    repo = repo.resolve()
    if git(repo, "status", "--porcelain").stdout.strip():
        raise RecoveryError("recovery apply requires a clean owned Implementation worktree")
    head = sha(repo, "HEAD")
    current_main = sha(repo, current_main_sha)
    if head != current_main:
        raise RecoveryError(
            f"recovery apply must start at exact current main {current_main}; owned worktree HEAD is {head}"
        )

    plan = build_plan(
        repo=repo,
        landed_sha=landed_sha,
        current_main_sha=current_main,
        current_main_ref=current_main_ref,
        known_residual_effects=known_residual_effects,
    )
    if plan.status != "candidate" or plan.landing_kind is None or plan.inverse_tree_sha is None:
        raise RecoveryError(plan.reason or "mechanical inverse is not a safe candidate")
    if expected_tree_sha is not None and expected_tree_sha != plan.inverse_tree_sha:
        raise RecoveryError(
            f"planned inverse tree moved: expected {expected_tree_sha}, recomputed {plan.inverse_tree_sha}"
        )

    inverse = git(repo, *inverse_args(plan.landing_kind, plan.landed_sha), check=False)
    if inverse.returncode != 0:
        git(repo, "revert", "--abort", check=False)
        raise RecoveryError(
            "mechanical inverse became conflicting during apply; return to semantic Implementation"
        )
    applied_tree = git(repo, "write-tree").stdout.strip()
    if applied_tree != plan.inverse_tree_sha:
        git(repo, "reset", "--hard", plan.current_main_sha, check=False)
        raise RecoveryError(
            f"applied inverse tree {applied_tree} does not match dry-run tree {plan.inverse_tree_sha}"
        )
    return plan
