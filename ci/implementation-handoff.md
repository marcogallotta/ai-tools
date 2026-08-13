# Canonical repository-changing implementation/fix handoff

This is the repository-owned source for every Dish handoff that authorizes an Implementation/fix agent to change `marcogallotta/ai-tools`. Coordinator, Development Workflow, Implementation, and any dispatcher producing such a handoff use this same template. Asana template tasks may mirror or link it, but must not be the sole policy source.

## Work

`<task-specific goal>`

## Required assignment identity

Every handoff must state all fields explicitly:

```text
Repository: marcogallotta/ai-tools
Asana task: <task GID>
Authorized branch: agent/<branch>
Base ref: <exact ref, normally refs/heads/main>
Base SHA: <exact 40-character SHA>
Existing PR: none | #<number> <URL>
Expected PR head: n/a | <exact 40-character SHA>
```

`Existing PR: none` requires `Expected PR head: n/a`. An existing PR requires its exact current head SHA.

This tuple is the implementation-lineage assignment. **Matching Asana task identity on another branch or PR does not authorize work on that lineage.** Do not select, adopt, push to, or modify another branch/PR merely because it references the same task. If live GitHub/Asana authority contradicts any required field, stop with the contradiction instead of choosing a lineage.

## Authority/bootstrap

- Use the exact supplied Git base as the authoring base. Do not silently substitute a newer or older commit.
- Read root `CLAUDE.md`, `dish/docs/agents/index.md`, the mapped standing role contract, and the owning Asana task/project before editing.
- Establish and record the exact Git base SHA and verify the assignment identity above before local work starts.
- Local Claude/Codex: use `tools/agent-worktree`; do not share a mutable checkout, create a competing worktree lifecycle, or commit directly to `main`.
- ChatGPT: repository-changing work requires durable Git branch + commit + PR identity through the connected GitHub authority. A local-agent identity file is not used for ChatGPT.
- Patch files are diagnostic/export artifacts only; they are not a valid primary implementation handoff.

## Scope

- `<exact task-specific scope>`

## Constraints / non-goals

- `<task-specific invariants>`
- The task brief defines the work boundary.
- Stop discovery once the owning mechanism needed for acceptance is identified.
- Adjacent findings become separate follow-up work rather than silent scope expansion.

## Known evidence / dependencies

- `<known evidence, parallel work, environment facts, ordering constraints>`

## Local dispatch ownership gate

Claude Code/Codex instances first create their unique local agent identity as required by [`../dish/docs/agents/identity.md`](../dish/docs/agents/identity.md). Before entering or modifying a worktree, acquire the exact task/assignment ownership claim.

For new work:

```sh
tools/agent-worktree start \
  --task <task_gid> \
  --branch agent/<authorized-branch> \
  --base-ref <exact-base-ref> \
  --base <exact-base-sha> \
  --pr none \
  --agent-id <local-agent-id>
```

For an explicitly handed-off existing PR with no local task state:

```sh
tools/agent-worktree adopt \
  --task <task_gid> \
  --branch agent/<authorized-branch> \
  --base-ref <exact-base-ref> \
  --base <exact-base-sha> \
  --pr <pr-number> \
  --expected-head <exact-pr-head> \
  --agent-id <local-agent-id>
```

The task-scoped lock and durable claim make acquisition atomic for this repository + task + assignment. If an active claim exists, a second local agent stops on the deterministic ownership collision; it must not enter the recorded worktree or operate independently. A different branch/base/PR for the same task is an assignment collision and is never silently adopted.

After new work publishes its branch and GitHub creates the PR, bind that PR once to the existing assignment:

```sh
tools/agent-worktree bind-pr \
  --task <task_gid> \
  --pr <pr-number> \
  --head <exact-pr-head> \
  --agent-id <local-agent-id>
```

`bind-pr` may fill the initial `PR none` identity only; it may not switch the task to another PR.

## Explicit owner transfer and stale-owner recovery

Local identity files and advisory GitHub lease markers are visibility/recovery evidence, not liveness authority. Do not infer that an owner is dead from age, mtime, a missing lease renewal, or silence.

When orchestration has explicitly established that ownership was handed off or the old local owner is abandoned/stale, read the current local task state and transfer ownership with an exact compare-and-set on its current claim:

```sh
tools/agent-worktree status --task <task_gid>
tools/agent-worktree resume \
  --task <task_gid> \
  --agent-id <replacement-agent-id> \
  --takeover \
  --expected-claim <exact-current-claim>
```

A changed claim fails without changing ownership. Legacy task state created before exclusive claims is recoverable only through the explicit sentinel `--expected-claim legacy-unclaimed`. Takeover never authorizes a different branch/base/PR tuple.

For publication-blocker/fix handoffs, reconcile this local claim before touching another session's prepared worktree. The durable PR handoff repeats the full assignment identity above plus the current PR head; if local ownership transfer is required, state it explicitly.

## Repository/test discipline

- Read the relevant subsystem docs and current implementation.
- Use governed planner/ownership metadata for the complete changed-path evidence set.
- Run evidence appropriate to the semantic delta and report only what actually ran.
- Do not continuously chase unrelated moving `main` during authoring.
- Keep evidence bound to the exact implementation/PR head where the standing contract requires it.

## Delivery

1. make coherent commit(s) on the owned branch;
2. publish only that authorized branch and verify its exact remote head;
3. open/update one draft PR early when useful for durable Git/PR identity;
4. finish the complete task-scoped implementation and required evidence while the PR may remain draft;
5. update the PR description with final evidence, limitations, exact base SHA, and exact current head SHA;
6. explicitly mark the PR ready for review;
7. verify GitHub reports `draft=false`;
8. return the review-ready PR URL + current PR head SHA + base SHA + evidence.

## Terminal handoff rule

`draft=true` is AUTHORING / NOT REVIEWABLE, not the normal terminal deliverable for completed repository-changing work. The normal terminal handoff is a review-ready merge candidate (`draft=false`). If the PR cannot become review-ready, return the concrete blocker and missing evidence instead of presenting a draft PR as completed delivery.

## Return

Return at least:

- result and whether the requested gap existed;
- PR URL and authorized branch name;
- exact base SHA, implementation commit SHA, and current PR head SHA;
- confirmation GitHub reports `draft=false`, or the concrete blocker preventing review-ready state;
- concise semantic summary;
- schema/migration changes, if any;
- changed files and why;
- governed tests/checks and results;
- environment limitations or missing certification;
- interactions with parallel unmerged work;
- adjacent discoveries explicitly excluded from this change.
