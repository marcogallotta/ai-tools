# Already-landed source recovery

Use `scripts/pr_source_recovery.py` only from an owned recovery Implementation branch based on exact current `main`. It prepares or applies a source-only inverse candidate; it never rewrites `main`, merges, performs Review, or claims runtime/database/deployment/external effects were reversed.

The helper verifies the landed commit against exact current `main` first-parent history before choosing the inverse. A one-parent squash/rebase landing uses an ordinary revert. A true two-parent merge uses parent 1 only after the merge itself is proven on current `main`'s first-parent chain. Unusual or ambiguous history fails closed to semantic Implementation.

`plan` dry-runs the inverse in a detached worktree at exact current `main`. Conflicts, an empty inverse, or current-main movement are not Integration work; they return to semantic Implementation. Later unrelated work remains in the candidate because the inverse is computed on current `main`, and paths touched by later commits are reported for Review attention.

`apply` requires a clean owned worktree whose HEAD is the same exact current-main SHA, recomputes the plan, optionally requires the precommitted inverse tree, and leaves the inverse staged only when the resulting tree matches the dry run exactly.

A clean inverse is still only an Implementation candidate. Publish it through the normal branch/commit/PR lifecycle and obtain independent exact-head Review that verifies the intended landed delta is removed while later unrelated behavior is preserved. Record known database/runtime/deployment/external effects as residual gates and recover them only through their existing authorities.
