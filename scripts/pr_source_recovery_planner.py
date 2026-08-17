from __future__ import annotations

from pathlib import Path
from typing import Iterable

from pr_source_recovery_history import (
    failure_plan,
    first_parent_chain,
    later_touching_paths,
    parents,
    paths_between,
)
from pr_source_recovery_inverse import dry_run_inverse
from pr_source_recovery_types import RecoveryError, RecoveryPlan, residual_effects, sha


def build_plan(
    *,
    repo: Path,
    landed_sha: str,
    current_main_sha: str,
    current_main_ref: str = "refs/remotes/origin/main",
    known_residual_effects: Iterable[str] = (),
) -> RecoveryPlan:
    repo = repo.resolve()
    landed = sha(repo, landed_sha)
    current_main = sha(repo, current_main_sha)
    authoritative_ref = sha(repo, current_main_ref)
    residual = residual_effects(known_residual_effects)
    if authoritative_ref != current_main:
        raise RecoveryError(
            f"current-main movement detected: {current_main_ref} is {authoritative_ref}, expected {current_main}"
        )

    commit_parents = parents(repo, landed)
    first_parent = first_parent_chain(repo, current_main)
    if landed not in first_parent:
        return failure_plan(
            repo=repo,
            landed_sha=landed,
            current_main_sha=current_main,
            current_main_ref=current_main_ref,
            commit_parents=commit_parents,
            landing_kind=None,
            mainline_parent=None,
            reason=(
                "landed commit is not on exact current main's first-parent history; "
                "landed identity or history shape is ambiguous"
            ),
            known_residual_effects=residual,
        )

    if len(commit_parents) == 1:
        landing_kind = "one-parent"
        mainline_parent = commit_parents[0]
    elif len(commit_parents) == 2:
        landing_kind = "true-merge"
        mainline_parent = commit_parents[0]
        if landed not in first_parent:
            return failure_plan(
                repo=repo,
                landed_sha=landed,
                current_main_sha=current_main,
                current_main_ref=current_main_ref,
                commit_parents=commit_parents,
                landing_kind=landing_kind,
                mainline_parent=None,
                reason="true merge mainline parent could not be verified against current main",
                known_residual_effects=residual,
            )
    else:
        return failure_plan(
            repo=repo,
            landed_sha=landed,
            current_main_sha=current_main,
            current_main_ref=current_main_ref,
            commit_parents=commit_parents,
            landing_kind=None,
            mainline_parent=None,
            reason=f"unsupported landed commit parent count {len(commit_parents)}",
            known_residual_effects=residual,
        )

    landed_paths = paths_between(repo, mainline_parent, landed)
    touching = later_touching_paths(
        repo,
        landed_sha=landed,
        current_main_sha=current_main,
        landed_paths=landed_paths,
    )
    return dry_run_inverse(
        repo=repo,
        landed_sha=landed,
        current_main_sha=current_main,
        current_main_ref=current_main_ref,
        commit_parents=commit_parents,
        landing_kind=landing_kind,
        mainline_parent=mainline_parent,
        landed_paths=landed_paths,
        later_touching=touching,
        known_residual_effects=residual,
    )
