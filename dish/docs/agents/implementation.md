# Implementation agent

This is the standing contract for Dish implementation and fix agents. All implementation work inherits [`contributor-base.md`](contributor-base.md). Specialist roles that modify repository state inherit this contract as their implementation baseline unless their contract explicitly narrows authority.

Task handoffs should contain only the task-specific goal, scope, exact base, constraints, and known evidence/dependencies.

Implementation/fix work is distinct from final local integration. Reviewed-patch application, local merge certification, promotion to `main`, push verification, and integration-worktree cleanup belong to [`integration.md`](integration.md) when the Integration role is assigned.

## Repository freshness

Do not continuously poll `origin` while implementing. Establish the base at task start and work against that known base.

Fetch/synchronize during implementation only when:

- starting or resuming a task after interruption;
- explicitly instructed to sync/rebase/merge;
- preparing integration handoff.

Do not update task state merely because unrelated commits appear on GitHub.

## Start from the supplied authority

Use the exact authoritative source supplied with the task. Report the exact source identity available for that authority (for example the supplied archive SHA-256 or an explicitly supplied commit identity). Do not invent a different source identity.

Before changing Dish code, follow root `CLAUDE.md` and start at `dish/docs/architecture/index.md` for subsystem routing.

Do not silently substitute another base or assume an unmerged parallel patch has landed.

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

Only the first two belong in the patch. Record adjacent findings separately as follow-up work.

Once the existing mechanism responsible for the requested invariant is identified, stop discovery and make the smallest change needed to enforce or prove that invariant.

Before adding new files, systems, targets, or process changes, ask whether the change directly satisfies the acceptance criteria. If it improves surrounding systems without being required, do not include it in the patch.

Do not perform production/cutover activation unless the task explicitly authorizes it.

## Parallel patches and migrations

An unmerged parallel patch is not part of your base.

If parallel patches independently claim the same migration number, do not invent prospective ordering unless the handoff explicitly establishes a semantic dependency. Keep your patch reviewable on its actual base. Migration renumbering can be resolved mechanically after one patch lands.

If integration later requires only mechanical renumber/rebase work, preserve semantics exactly and say so. If conflict resolution requires a real schema/code decision, report that as a semantic change.

## Evidence

Use `dish/test_selection/ownership.csv`, the test planner, and the repository testing policy for the complete changed-path set.

Run focused deterministic evidence appropriate to the semantic delta. Evidence strength must match the mechanism:

- SQLite/PGlite does not certify native PostgreSQL locking/isolation;
- unit/static tests do not certify browser behavior;
- process/restart guarantees need the relevant real boundary.

Do not claim tests that did not run.

A venv is not part of the handoff by default. Build/use the environment according to root `CLAUDE.md`. If a required environment-specific guarantee cannot be exercised, state the exact missing certification.

Do not rerun large suites merely to produce volume when existing focused evidence plus governed lanes establish the changed behavior, but follow repository requirements for completed change blocks.

## Return contract

Return enough information for the coordinator to review without reconstructing your work:

1. result and whether the requested gap existed on the supplied base;
2. patch/download artifact and SHA-256;
3. exact source/base identity;
4. concise semantic summary;
5. schema/migration changes, if any;
6. exact changed files;
7. tests/checks run and results;
8. environment limitations and exact missing certification;
9. any known interaction with parallel unmerged work;
10. whether any part of the patch is mechanical-only versus semantic.

Do not describe work as merged, landed, deployed, or activated unless you actually have authoritative evidence of that state.

If you are returning a fix requested by a reviewer, address the reviewer's exact blocker scope and identify any additional semantic changes you had to make.
