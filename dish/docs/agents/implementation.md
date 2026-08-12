# Implementation agent

This is the standing contract for Dish implementation and fix agents. All implementation work inherits [`contributor-base.md`](contributor-base.md). Specialist roles that modify repository state inherit this contract as their implementation baseline unless their contract explicitly narrows authority.

Task handoffs should contain only the task-specific goal, scope, exact base, constraints, and known evidence/dependencies.

Implementation/fix work is distinct from review and final integration. The canonical lifecycle for new work is:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub branch/commit/PR identity is the authoritative code artifact and review surface. Asana is an orchestration/status surface; it may record links and state, but it is never the source artifact for code review or integration.

## Repository freshness

Do not continuously poll `origin` while implementing. Establish the base at task start and work against that known base.

Fetch/synchronize during implementation only when:

- starting or resuming a task after interruption;
- explicitly instructed to sync/rebase/merge;
- preparing the PR/review handoff.

Do not update task state merely because unrelated commits appear on GitHub.

## Start from the supplied authority

Use the exact authoritative source supplied with the task. For repository work, record the exact base commit SHA. Do not invent a different source identity.

Before changing Dish code, follow root `CLAUDE.md` and start at `dish/docs/architecture/index.md` for subsystem routing.

Do not silently substitute another base or assume unmerged parallel work has landed.

## Branch and worktree ownership

New implementation work uses an owned branch. Do not commit directly to `main` by default.

Day-one branch rules:

- name agent-created branches `agent/<short-task-slug>` unless the handoff establishes another repository convention;
- one implementation agent owns the branch while semantic implementation is in progress;
- another agent must not push semantic changes to that branch without an explicit handoff of ownership;
- Claude Code/Codex local work should use a dedicated worktree when concurrent repository work could otherwise share an index or working tree;
- ChatGPT uses the connected GitHub integration as repository source/history authority and may perform the branch/commit/PR flow through connector-native GitHub operations;
- do not reuse a branch whose PR was merged, closed, abandoned, or superseded for unrelated work;
- stale-branch cleanup is manual day-one hygiene. Cleanup automation is future work; never delete a branch that may be the only recoverable copy of unlanded work.

Marco may explicitly authorize an emergency direct-to-`main` commit. That override must be stated explicitly for the specific change; it is not a standing shortcut and does not silently waive required validation or review evidence unless Marco says so.

## Canonical PR workflow

For new work:

1. create or take ownership of the implementation branch from the exact supplied base;
2. make the smallest coherent change that satisfies the task;
3. run the applicable evidence for the complete changed-path set;
4. commit the intended files with a concise commit message;
5. push/publish the branch to GitHub;
6. open a **draft pull request** against the intended base branch unless the handoff explicitly requires a ready-for-review PR;
7. verify the PR's current head SHA from GitHub;
8. return the PR URL, branch name, implementation commit SHA, and PR head SHA together with the evidence and semantic summary.

The PR is the review surface. Do not create a patch file or patch-only handoff for new work.

Host tooling differs, but the artifact contract does not:

- **ChatGPT:** use the connected GitHub integration as source/history authority and use connector-native branch/commit/PR operations when available;
- **Claude Code/Codex:** use the live checkout plus host-native `git`/worktree tooling, then push the owned branch and open/update the GitHub PR.

Regardless of host, the coordinator/reviewer/integrator must be able to identify the same branch, commit, PR URL, and exact PR head SHA.

## Scope and authority

Implement the smallest coherent change that satisfies the stated task.

Preserve established authority and identity boundaries. Do not introduce a second:

- durable decision/writer authority;
- replay/request identity;
- workflow-legality authority;
- effect-retry authority;
- lease authority;
- canonical writer.

When a dependency, architectural contradiction, or necessary scope expansion appears, report it rather than silently broadening the task.

### Scope discipline

The task brief defines the implementation boundary. Do not expand an implementation task into an audit, redesign, inventory exercise, or architecture change unless the task explicitly requests it.

During investigation, separate:

- facts required to implement the stated change;
- evidence required to prove the stated invariant;
- adjacent findings that may be useful but are outside scope.

Only the first two belong in the PR. Record adjacent findings separately as follow-up work.

Once the existing mechanism responsible for the requested invariant is identified, stop discovery and make the smallest change needed to enforce or prove that invariant.

Before adding new files, systems, targets, or process changes, ask whether the change directly satisfies the acceptance criteria. If it improves surrounding systems without being required, do not include it in the PR.

Do not perform production/cutover activation unless the task explicitly authorizes it.

## Parallel branches and migrations

An unmerged parallel PR is not part of your base.

If parallel branches independently claim the same migration number, do not invent prospective ordering unless the handoff explicitly establishes a semantic dependency. Keep your PR reviewable on its actual base. Migration renumbering can be resolved mechanically after one PR lands.

If integration later requires only mechanical renumber/rebase work, preserve semantics exactly and say so. If conflict resolution requires a real schema/code/product decision, it is semantic work and must return to the implementation/review path rather than being improvised by the integrator.

## Review-head changes

The PR head SHA is the review identity.

If you push new commits after review:

- update the reported PR head SHA;
- identify whether the new commit is semantic or mechanical-only;
- semantic changes require substantive re-review of the new head;
- a genuinely mechanical-only update still requires an explicit exact-head mechanical recheck before integration; an approval of an older head is not silently transferred to a different SHA.

Do not force-push or rewrite reviewed branch history unless the coordinator explicitly requires it and the resulting new head will be treated as a new review identity.

## Evidence and checks

Use `dish/test_selection/ownership.csv`, the test planner, and the repository testing policy for the complete changed-path set.

Run focused deterministic evidence appropriate to the semantic delta. Evidence strength must match the mechanism:

- SQLite/PGlite does not certify native PostgreSQL locking/isolation;
- unit/static tests do not certify browser behavior;
- process/restart guarantees need the relevant real boundary.

Do not claim tests that did not run.

A venv is not part of the handoff by default. Build/use the environment according to root `CLAUDE.md`. If a required environment-specific guarantee cannot be exercised, state the exact missing certification.

Until PR-triggered CI is integrated for this workflow, `checks` means the existing manual certification/test evidence applicable to the change. Do not imply that a green GitHub Checks surface exists when the evidence was run manually. Future CI should certify the exact PR head SHA.

Do not rerun large suites merely to produce volume when existing focused evidence plus governed lanes establish the changed behavior, but follow repository requirements for completed change blocks.

## Migration from patch handoffs

- New work uses the PR workflow.
- Existing patch-based work already in flight may finish under the old flow or be converted into a branch/commit/PR.
- Do not create a new patch-only handoff.
- When converting legacy patch work, preserve its provenance in the PR description or coordination record, but review/integration proceeds using the PR head SHA as the active identity.

## Return contract

Return enough information for the coordinator/reviewer to proceed without reconstructing your work:

1. result and whether the requested gap existed on the supplied base;
2. PR URL;
3. owned branch name;
4. exact implementation commit SHA and current PR head SHA;
5. exact source/base commit SHA;
6. concise semantic summary;
7. schema/migration changes, if any;
8. exact changed files;
9. tests/checks run and results;
10. environment limitations and exact missing certification;
11. any known interaction with parallel unmerged work;
12. whether any post-review or integration-relevant change is mechanical-only versus semantic.

Do not describe work as merged, landed, deployed, or activated unless you actually have authoritative evidence of that state.

If you are returning a fix requested by a reviewer, update the existing PR unless the coordinator explicitly requires a replacement PR, address the reviewer's exact blocker scope, identify any additional semantic changes, and return the new exact PR head SHA.
