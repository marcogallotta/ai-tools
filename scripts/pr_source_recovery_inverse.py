from __future__ import annotations

from pathlib import Path
import tempfile

from pr_source_recovery_history import failure_plan
from pr_source_recovery_types import RecoveryPlan, git, inverse_args


def dry_run_inverse(
    *,
    repo: Path,
    landed_sha: str,
    current_main_sha: str,
    current_main_ref: str,
    commit_parents: tuple[str, ...],
    landing_kind: str,
    mainline_parent: str,
    landed_paths: tuple[str, ...],
    later_touching: tuple[str, ...],
    known_residual_effects: tuple[str, ...],
) -> RecoveryPlan:
    with tempfile.TemporaryDirectory(prefix="dish-source-recovery-") as temp:
        worktree = Path(temp) / "worktree"
        git(repo, "worktree", "add", "--detach", "--quiet", str(worktree), current_main_sha)
        try:
            inverse = git(worktree, *inverse_args(landing_kind, landed_sha), check=False)
            if inverse.returncode != 0:
                conflicts = tuple(
                    sorted(
                        line.strip()
                        for line in git(
                            worktree, "diff", "--name-only", "--diff-filter=U", check=False
                        ).stdout.splitlines()
                        if line.strip()
                    )
                )
                return failure_plan(
                    repo=repo,
                    landed_sha=landed_sha,
                    current_main_sha=current_main_sha,
                    current_main_ref=current_main_ref,
                    commit_parents=commit_parents,
                    landing_kind=landing_kind,
                    mainline_parent=mainline_parent,
                    changed_paths=landed_paths,
                    later_touching_paths=later_touching,
                    conflict_paths=conflicts,
                    reason=(
                        "mechanical inverse conflicts on exact current main"
                        + (f": {', '.join(conflicts)}" if conflicts else "")
                    ),
                    known_residual_effects=known_residual_effects,
                )
            changed = tuple(
                sorted(
                    line.strip()
                    for line in git(worktree, "diff", "--cached", "--name-only").stdout.splitlines()
                    if line.strip()
                )
            )
            if not changed:
                return failure_plan(
                    repo=repo,
                    landed_sha=landed_sha,
                    current_main_sha=current_main_sha,
                    current_main_ref=current_main_ref,
                    commit_parents=commit_parents,
                    landing_kind=landing_kind,
                    mainline_parent=mainline_parent,
                    changed_paths=landed_paths,
                    later_touching_paths=later_touching,
                    reason="mechanical inverse produced no source delta on exact current main",
                    known_residual_effects=known_residual_effects,
                )
            tree = git(worktree, "write-tree").stdout.strip()
        finally:
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)

    return RecoveryPlan(
        schema="dish-source-recovery-plan-v1",
        status="candidate",
        repository_path=str(repo),
        landed_sha=landed_sha,
        current_main_sha=current_main_sha,
        current_main_ref=current_main_ref,
        landing_kind=landing_kind,
        landed_parents=commit_parents,
        mainline_parent=mainline_parent,
        changed_paths=changed,
        later_touching_paths=later_touching,
        conflict_paths=(),
        inverse_tree_sha=tree,
        reason=None,
        source_reversal_scope="git-source-only",
        runtime_effects_reversed=False,
        known_residual_effects=known_residual_effects,
        next_action=(
            "apply this exact candidate on the owned recovery Implementation branch, publish it, "
            "then obtain independent exact-head Review"
        ),
    )
