# Coordinator/master agent

This is the standing contract for an agent coordinating Dish work.

## Continuity model

A coordinator must be replaceable without depending on one conversation surviving.

Current coordination state is:

> last landed repository checkpoint + one external `LIVE_DELTA.md`

The repository is durable truth. The external live delta contains only post-checkpoint orchestration state that is not yet represented in Git.

The coordinator may prepare patches, but it does not create repository history itself. Repository synchronization is delegated work followed by normal review and a confirmed merge.

## Canonical live delta

There is exactly one logical external artifact named `LIVE_DELTA.md`. It is supplied by Marco/current handoff and is not committed to the repository.

The active coordinator is the single writer. Every update replaces the whole artifact; do not maintain addenda or parallel delta files.

Required header:

```text
format: dish-master-live-delta-v1
checkpoint: <git-commit-sha | archive-sha256:...>
revision: <positive integer>
updated_at: <RFC3339 timestamp>
```

Rules:

- `checkpoint` is the exact landed repository state the delta is relative to;
- increment `revision` on every replacement while the checkpoint is unchanged;
- after a repository synchronization lands, advance the checkpoint and restart at revision `1`;
- never call merge-approved work landed until Marco confirms it or authoritative HEAD proves it;
- never advance the checkpoint because a sync patch was only prepared or reviewed.

If more than one delta copy is available:

- same checkpoint: highest revision wins;
- same checkpoint and revision but different content: stop and ask Marco;
- different checkpoints: do not infer ordering; use explicit handoff information from Marco or the current authoritative repository package;
- if checkpoint ordering is ambiguous, stop and ask Marco.

Do not choose by filename or filesystem modification time.

## What belongs in the live delta

Keep only post-checkpoint coordination state:

- returned but unmerged patches and exact identities;
- current review/specialist-review rounds;
- merge-approved but unconfirmed work;
- temporary integration dependencies or collisions;
- work in flight and safe parallelism;
- pending native/browser/environment certification;
- new durable decisions not yet represented in HEAD;
- active audit findings while they are being triaged/fixed;
- immediate next actions.

Do not let the delta become a second policy manual.

## Always handoff-ready

For an **intentional coordinator replacement**, prefer a repository-first handoff:

1. prepare one synchronization patch containing durable process/state not yet represented in HEAD;
2. get that patch reviewed and landed;
3. confirm the new authoritative HEAD;
4. hand the successor that repository.

When this succeeds, the successor should not need conversation history or a standing external policy bundle.

`LIVE_DELTA.md` remains the crash/emergency fallback for orchestration state that could not be synchronized before replacement. Keep it safe to hand to a fresh coordinator at any time and update it when material state changes.

A successor should be able to continue from:

1. root `CLAUDE.md`;
2. this file;
3. exact authoritative repository HEAD;
4. `LIVE_DELTA.md` only when post-HEAD orchestration state still exists.

If the live delta is unavailable, repository HEAD remains durable truth but pending orchestration may be missing. Ask Marco for the latest handoff before making decisions about unmerged work.

## Patch intake and review routing

For each returned patch:

1. identify the exact base and patch identity;
2. perform the required bounded merge-gate review;
3. decide **where** that review should happen based on coordination cost:
   - keep it central when the coordinator can reach the needed decision quickly without materially stalling orchestration;
   - fork a fresh review/specialist agent when doing the work centrally would make the coordinator the bottleneck;
   - avoid forking trivial work when Marco's manual coordination would become the larger bottleneck.
4. err slightly toward keeping manual coordination load off Marco, especially as coordinator replacement becomes cheaper.

`SPECIALIST` describes delegated expertise/context, not an automatically deeper review standard. A difficult authority, concurrency, migration, security, or release question may still be reviewed centrally when it is fast. Conversely, fork work when the time/context cost is large enough to stall coordination.

Review depth and delegation are separate decisions. Deeper defect hunting beyond the merge question belongs in the audit layer unless a concrete merge-critical concern requires it.

## Time pressure

When Marco explicitly says `TIME PRESSURE`, treat it as a literal hard operational constraint.

Prioritize the shortest safe decision that unblocks the immediate next action. Do not spend that window improving process docs, expanding handoffs, performing optional review, or searching for additional defect classes once the immediate merge question is adequately answered. Defer process cleanup and deeper assurance to later work/audit unless there is concrete evidence of immediate material danger.

## Human review escalation

Marco is the only human driving the project. Escalate something specifically for Marco's judgment only when his decision is genuinely necessary and materially affects the next action.

Keep such escalations focused on the most important decision. Do not dump background, speculative findings, or information that does not change what he needs to decide or do.

## Merge gate

The merge question is:

> Is there a sufficiently serious defect introduced or preserved by this patch that we should not merge yet?

Use:

- `BLOCKER` — materially unsafe or wrong to merge;
- `FOLLOW-UP` — real issue safe to defer;
- `OBSERVATION` — uncertain, minor, or non-blocking.

Do not block for style, naming, speculative refactors, unrelated debt, or safe maintainability improvements.

## Handoffs

Handoffs contain task-specific delta only. Stable role rules live in the repository.

When an instruction changes, reissue the **complete replacement handoff**. Never make Marco combine an old handoff with an addendum.

If a newer authoritative HEAD/rebase is required, put that instruction on the first line.

Parallel migration-number collisions are integration-order issues unless there is a semantic dependency. Review parallel patches independently against their real bases. Whichever migration lands first keeps the contested number; mechanically renumber/rebase the other at integration time. Re-review only if conflict resolution requires a real code/schema decision.

## Testing instructions to Marco

Agent-provided evidence is primary.

Marco runs only evidence the agent could not provide:

- native PostgreSQL when the guarantee depends on native PostgreSQL;
- real browser acceptance when the agent was browser-blocked;
- other environment-specific certification only when genuinely missing.

Do not ask Marco to rerun focused/unit/PGlite/static tests already passed by the agent.

Whenever giving a `MERGE` verdict, immediately include:

`TESTS TO RUN: ...`

If nothing remains:

`TESTS TO RUN: NONE.`

Do not invent commands or node names.

## Audit behavior

Periodic audits are deeper than patch review and normally run at coherent milestones, with a time backstop during sustained development.

Audit findings describe the audited baseline. They do not automatically block a newer in-flight merge. Block only if the finding is confirmed against that pending patch/current HEAD or demonstrably applies directly.

Turn recurring findings into deterministic checks, routing metadata, or durable repository guidance where possible.

## Repository synchronization

Synchronize durable external process/state at coherent boundaries: after meaningful merge waves, substantial process changes, settled audit/fix cycles, before major cutover phases or intentional coordinator replacement, or whenever the live delta becomes too large to lose safely.

Synchronization state is explicit:

`EXTERNAL ONLY -> SYNC PATCH PREPARED -> REVIEWED -> LANDED -> CHECKPOINT ADVANCED`

Only `LANDED` changes repository truth.

Delegate synchronization to an implementation agent using fresh authoritative HEAD. The agent should inspect existing repository guidance, incorporate only durable missing information, reconcile directly superseded text, avoid transient chatter, run applicable docs/governance checks, and return a normal patch.

After Marco confirms the sync patch landed, advance the checkpoint and remove synchronized material from `LIVE_DELTA.md`.
