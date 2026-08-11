# Implementation agent

This is the standing contract for Dish implementation and fix agents. Task handoffs should contain only the task-specific goal, scope, exact base, constraints, and known evidence/dependencies.

## Start from the supplied authority

Use the exact authoritative HEAD/archive supplied with the task. Report its Git commit when available; otherwise report the authoritative archive hash and say that Git metadata is unavailable.

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
