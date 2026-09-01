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

For a local first claim, the same durable handoff record is the independent Ready-admission
witness. Use this exact host-specific source shape for new work:

```text
dish-prelaunch:v1 repository=marcogallotta/ai-tools task=<gid> assignment=implementation host=local branch=<authorized-branch> base_ref=<base-ref> base_sha=<base-sha> existing_pr=none
```

For an existing PR, replace the tail with
`existing_pr=<number> expected_head=<exact-head>`. `agent-worktree` re-reads the live task and
stories, recomputes the handoff identity, and binds the exact assignment before first claim. The
local writer cannot mint this witness, and Ready without exactly one current matching witness is
not repository-mutation authority.

Post-PR fix provenance is separate: the manual handoff binds the live owning task, existing PR,
branch, blocked head, formal BLOCK review id, selected Implementation host, and expected successor
readback. No dispatcher, broker record, or automated consumer is required for the ordinary manual
Worker continuation.

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

## Review V4 chain of custody

Review V4 preserves the Review V3 chain of custody and extends it to the complete still-applicable governing set. When the governing accepted design/specification defines direct Marco intent, source-indexed Intent Coverage, protected invariants, solution envelope, signed Review challenges, or candidate Review Focus, the durable Implementation handoff is a **derived projection** of that exact authority. It must carry the material subset required by the implementation slice without weakening, paraphrasing exact Marco wording, silently narrowing the governing generation, or dropping inherited accepted requirements.

Add these fields when applicable:

```text
Governing design/spec generation: <exact generation id>
Governing generation digest: <exact sha256>
Marco Intent Baseline refs: <exact durable human-decision refs>
Intent Coverage refs: <stable IDs + source pointers/statuses for material active intent>
Intent delta disposition: <PRESERVE | REFINE | ADD | CHANGE | REMOVE/SUPERSEDE; approval ref for CHANGE/REMOVE of direct Marco intent>
Approved headline: <exact approved words, or none/not applicable>
Headline approval evidence: <exact durable Marco approval ref, or none/not applicable>
Implementation slice / accepted clauses: <exact bounded scope>
Protected invariants: <stable IDs + material statements/evidence refs>
Expected solution envelope: <material shape/surface/non-complexity constraints>
Review Focus / signed challenges: <material applicable challenges; no verdict steering>
```

The approved headline field is never inferred from `Has Headline`; exact-word durable human evidence is required whenever an approved headline is claimed. A later implementation summary, PR body, or handoff cannot become a competing intent/specification authority.

Immediately before material pre-development dispatch, Coordinator performs the Review V4 fast final admission check against the current exact governing generation/digest, fresh independent Design Review identity, durable human approval provenance, source-indexed intent/invariants and exact supplied/approved Marco wording, current `Needs Human Review` revision, outgoing handoff, and any later supersession/contradiction/dependency. Zero semantic dispatch occurs if the handoff materially omits, weakens, contradicts, rewrites, generalizes, or silently narrows governing material, or if an unapproved direct-human `CHANGE`/`REMOVE/SUPERSEDE` exists. Surface the exact delta and repair through existing authority; do not redesign silently in the handoff.

Semantic Code Review later identifies and reads this exact durable handoff in addition to the live owning task and governing accepted generation. Review independently checks **HANDOFF FIDELITY** and **IMPLEMENTATION CONFORMANCE**; faithful implementation of a drifted handoff is still a Review defect.

## External/current-main defect admission

Use this section only when a defect is discovered while pursuing an already-authorized operator objective. A real defect does not automatically become a prerequisite task, branch, or PR.

Before emitting new blocking Implementation work, record:

```text
Originating objective: <exact requested next state>
Necessity disposition: <CONTINUE_ORIGINAL | IMPLEMENTATION_REQUIRED | UNCERTAIN>
Necessity evidence: <why current authorized mechanisms can/cannot advance the objective>
Owner/lineage disposition: <CONTINUE_EXISTING_LINEAGE | REUSE_OWNER_NEW_LINEAGE | CREATE_BOUNDED_OWNER_LINEAGE | ADD_TO_COHERENT_WORKSTREAM | not-applicable>
Owner/lineage evidence: <live Asana owner + live GitHub branch/PR reconciliation, or why not applicable>
```

Stage 1 is goal continuity. `CONTINUE_ORIGINAL` continues the original objective and keeps the defect non-prerequisite; `UNCERTAIN` requires the smallest continuation/recovery/reconciliation investigation first. Only `IMPLEMENTATION_REQUIRED` proceeds to Stage 2.

Stage 2 resolves the correct role/domain and live owner/lineage before authoring/publication. `ADD_TO_COHERENT_WORKSTREAM` is valid only when a durable current workstream/member mapping already exists. Do not force unrelated defects into a mega-PR, create a second scheduler/queue/control plane, or ask Marco to choose routine lineage mechanics that live authority resolves. Preserve technically sound already-authored work by reconciling provenance when safe rather than discarding it for aesthetic history.
