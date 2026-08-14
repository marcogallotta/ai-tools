# Repository implementation handoff

This file is the **single canonical repository-owned contract** for every repository-changing
Implementation/fix assignment, including local publication completion. Coordinator, Development
Workflow, Implementation, the role index, and lifecycle-dispatch documentation all point here.
Asana may mirror or link this contract, but it is not the sole policy source.

## Assignment identity

Every handoff states this tuple explicitly:

```text
Repository: marcogallotta/ai-tools
Asana task GID: <exact gid>
Authorized branch: agent/<short-task-slug>
Base ref: refs/heads/<target>
Base SHA: <exact 40-char sha>
Existing PR: <#number or none>
Expected PR head: <exact 40-char sha when Existing PR is set; otherwise none>
Assignment class: <implementation | fix | local implementation completion>
```

The repository + Asana task GID + authorized branch + existing-PR/expected-head tuple is one
assignment identity. A matching task on another branch or PR is **not authorization** to adopt,
write, publish, or complete that other lineage. If any element disagrees with live GitHub or durable
local worktree state, stop and reconcile the contradiction rather than choosing a lineage from
memory or convenience.

The supplied base is the authoring base. Later target-branch movement is observed at normal
resume/handoff boundaries; it does not silently replace the base or authorize rebase/reset.

The global Implementation claim is part of this same assignment identity. Fresh work has no prior
global generation and acquires one before semantic authoring. Continuation/replacement records the
exact current `global_claim_id`; a stale generation is never silently adopted from Asana, GitHub, or
a local worktree.

## Local dispatch ownership

Claude Code/Codex repository writers acquire the matching exclusive local claim **before** touching
branch/worktree state or executing the implementation agent. `tools/agent-worktree claim` is the
only dispatch/start ownership gate; writer subcommands run inside it.

New work:

```sh
tools/agent-worktree claim \
  --task <gid> \
  --branch agent/<short-task-slug> \
  --agent-id <local-agent-id> \
  --base <exact-base-sha> \
  -- \
  tools/agent-worktree start \
    --task <gid> \
    --branch agent/<short-task-slug> \
    --base-ref <exact-base-ref> \
    --base <exact-base-sha> \
    --agent-id <local-agent-id>
```

Explicitly handed-off existing PR:

```sh
tools/agent-worktree claim \
  --task <gid> \
  --branch agent/<short-task-slug> \
  --agent-id <local-agent-id> \
  --base <exact-base-sha> \
  --pr-number <pr-number> \
  --pr-head <exact-pr-head> \
  --pr-lease-state <active|none> \
  [--pr-lease-id <lease-uuid>] \
  -- \
  tools/agent-worktree adopt \
    --task <gid> \
    --branch agent/<short-task-slug> \
    --base-ref <exact-base-ref> \
    --base <exact-base-sha> \
    --expected-head <exact-pr-head> \
    --agent-id <local-agent-id>
```

The global generation prevents a second host/session from receiving writable task authority; the
local locks prevent two same-host processes from concurrently using the one winning generation. PR
`dish-agent-lease` markers remain advisory visibility evidence only; they are never sole ownership
authority. Every dispatcher checks the global dispatch guard first and fails closed when the claim
service or exact Asana mirror/readback is unavailable.

## Explicit owner transfer and stale-owner recovery

Do not infer that an owner is dead from age, mtime, silence, or advisory PR-lease age. A still-live
owner is protected by the live claim locks and cannot be bypassed with takeover.

When orchestration has explicitly established a handoff or abandoned/stale prior owner, first read
the exact current durable claim generation:

```sh
tools/agent-worktree status --task <gid> --json
```

For claimed state, use both `claim.global_claim_id` (cross-host durable generation) and
`claim.claim_id` (same-host process generation) from that output as exact compare-and-set values.
The replacement also carries an explicit takeover reason and bounded liveness evidence; claim age
alone is never sufficient. Then acquire the replacement claim and resume the preserved worktree in
one claimed process:

```sh
tools/agent-worktree claim \
  --task <gid> \
  --branch agent/<short-task-slug> \
  --agent-id <replacement-agent-id> \
  --takeover \
  --expected-global-claim <exact-current-global-claim-id> \
  --takeover-reason <explicit-handoff-or-recovery-reason> \
  --liveness-evidence <bounded-current-evidence> \
  --expected-claim <exact-current-local-claim-id> \
  [--pr-number <pr-number> --pr-head <exact-pr-head> --pr-lease-state <active|none> [--pr-lease-id <lease-uuid>]] \
  -- \
  tools/agent-worktree resume \
    --task <gid> \
    --agent-id <replacement-agent-id> \
    --takeover
```

The task/branch/PR locks serialize same-host takeover acquisition. `--expected-claim` is checked
against the current local process generation while those locks are held **before** the global CAS is
attempted, so a bad local takeover cannot advance cross-host ownership. The global service then
compares `--expected-global-claim` atomically and creates a fresh opaque generation. If either value
changed, takeover fails without granting writable authority; old global ids are ABA-safe and cannot
be reused after a later generation. An unresolved publication intent must be reconciled or explicitly
aborted before takeover, so no previously authorized branch-write window survives ownership transfer.

Legacy active worktree state created before exclusive claim records is recoverable only after
explicit orchestration handoff with `--expected-claim legacy-unclaimed`. The sentinel is accepted
only when active task state exists and no current claim record exists.

Replacement/fix/publication-completion agents reconcile and claim the **same assignment identity**
before touching preserved local state. Takeover never authorizes a different branch/base/PR tuple.

## Task-specific delta

Populate only the task-specific goal, scope, constraints, and evidence requirements in the calling
handoff. Stable role/process policy stays in the standing contracts.

```text
Goal: <exact outcome>
In scope: <bounded files/subsystem/behavior>
Out of scope: <material exclusions>
Required evidence: <focused tests/checks/environment evidence>
Known dependencies/overlap: <parallel PRs or none>
```

For `local implementation completion`, the existing draft PR must already contain the full
publication-blocker handoff required by `implementation.md`. Continue the same authorized branch and
PR only; do not create a replacement lineage.
