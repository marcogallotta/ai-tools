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
Implementation host: <CHATGPT_IMPLEMENTATION | LOCAL_IMPLEMENTATION>
```

When initial pre-PR orchestration selects `CHATGPT_IMPLEMENTATION` and later Review routing may use
that fact, the **independent pre-launch authority** is the durable Asana operator-handoff record
written by `scripts/operator_handoff.py` before the writer is launched. Use target role
`Implementation`, record the known branch/base with `PR: not yet known` and `Head: not yet known`,
require its normal state + story readback, and use this exact source string (single spaces, field
order unchanged):

```text
dish-prelaunch:v1 repository=marcogallotta/ai-tools task=<gid> assignment=implementation host=chatgpt branch=<authorized-branch> base_ref=<base-ref> base_sha=<base-sha> existing_pr=none
```

The returned 16-hex handoff identity is the launcher lineage: `asana-handoff:<handoff-id>`. Do not
accept a caller-chosen launcher label as provenance. After the branch has a PR and exact head,
`pr-implementation-provenance.yml` may publish a restart/cache record for that exact head using the
task, handoff id, base ref and base SHA. Its PR marker has this shape:

```text
<!-- dish-implementation-host-witness:v1 head=<exact-sha> host=chatgpt source=orchestration launcher=asana-handoff:<handoff-id> task=<gid> handoff=<handoff-id> base_ref=<base-ref> base_sha=<base-sha> run=<run-id> attempt=<n> artifact=<id> digest=<sha256> -->
```

The workflow artifact/comment is **not independent authority**. Review routing accepts it only after
mechanically re-reading the owning task's Asana stories, finding exactly one matching
`dish-implementation-handoff:v1` record, recomputing its handoff identity from the canonical source
above, verifying the selected ChatGPT host/assignment/branch/base, and confirming that durable
record predates the cache workflow run. Missing Asana read authority, a missing/duplicate/tampered
record, mismatched task/branch/base/host, or a cache-only/self-asserted marker yields no positive
remote witness and therefore falls back to ChatGPT Review. The record proves routing provenance
only; it never grants branch/write/Review authority.

Post-PR fix provenance is separate: it is produced by the lifecycle dispatcher only after a #95
broker-proven consumer returns and the proof-backed terminal broker event binds both accepted host
and exact result head.

The repository + Asana task GID + authorized branch + existing-PR/expected-head tuple is one
assignment identity. A matching task on another branch or PR is **not authorization** to adopt,
write, publish, or complete that other lineage. If any element disagrees with live GitHub or durable
local worktree state, stop and reconcile the contradiction rather than choosing a lineage from
memory or convenience.

The supplied base is the authoring base. Later target-branch movement is observed at normal
resume/handoff boundaries; it does not silently replace the base or authorize rebase/reset.

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

A live claim prevents a second local agent from independently writing the same task/branch/PR. PR
`dish-agent-lease` markers remain advisory visibility evidence only; they are never sole ownership
authority.

## Explicit owner transfer and stale-owner recovery

Do not infer that an owner is dead from age, mtime, silence, or advisory PR-lease age. A still-live
owner is protected by the live claim locks and cannot be bypassed with takeover.

When orchestration has explicitly established a handoff or abandoned/stale prior owner, first read
the exact current durable claim generation:

```sh
tools/agent-worktree status --task <gid> --json
```

For claimed state, use `claim.claim_id` from that output as the exact compare-and-set value. Then
acquire the replacement claim and resume the preserved worktree in one claimed process:

```sh
tools/agent-worktree claim \
  --task <gid> \
  --branch agent/<short-task-slug> \
  --agent-id <replacement-agent-id> \
  --takeover \
  --expected-claim <exact-current-claim-id> \
  [--pr-number <pr-number> --pr-head <exact-pr-head> --pr-lease-state <active|none> [--pr-lease-id <lease-uuid>]] \
  -- \
  tools/agent-worktree resume \
    --task <gid> \
    --agent-id <replacement-agent-id> \
    --takeover
```

The task/branch/PR locks serialize takeover acquisition. `--expected-claim` is checked against the
current durable claim while those locks are held, before a new ownership generation is written. If
another generation intervened, takeover fails with `OWNER_CLAIM_CHANGED`; an old claim id cannot be
reused after a later generation (ABA-safe because each generation has a new opaque claim id).

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
