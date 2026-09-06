# Recovery, observability, and completion

## Read this when

Read this when changing restart recovery, ambiguous-effect handling, lifecycle visibility, cleanup, rollout tracking, or completion criteria.

## Scope

This document records recovery and terminal-state boundaries. Operational retry commands and dashboards belong to their runbooks.

## Current architecture

The development system recovers from durable GitHub, Asana, repository policy, and local Git/worktree state. After restart or session replacement, the acting role reconstructs current state and routes only after fresh authority reads; there is no authoritative queue database or active background dispatcher. Local Implementation and Integration use external durable state for exact lineage recovery, but those records never create assignment authority.

A current execution may represent a role result as terminal only after the active role/procedure's already-required durable completion artifacts and any same-turn projection/readback needed to make that result consumable have been written and authoritatively read back. This is a response-eligibility boundary, not a universal artifact schema or new writer: each role/procedure continues to own its concrete durable completion truth. If safe authorized work or a fallback remains, the execution continues; exhausted routes may end in an exact blocker without a success claim. Stronger lifecycle claims and claims of separate ongoing work likewise require their separately owned evidence.

Ambiguous state-changing outcomes are reconciled before replay. A proven present effect is not repeated; a proven absent safe/idempotent effect may receive a bounded retry; unresolved ambiguity fails closed. Advisory activity signals help avoid duplicate work without becoming ownership or liveness proof.

For assigned-task dependency repair, recovery follows the same rule at edge granularity: reread the
exact task immediately before mutation, discard and recompute a stale dependency plan if the set
moved, remove only still-present mechanically satisfied edges, and reconcile partial/ambiguous writes
from authoritative final readback. A remaining unresolved edge stays a blocker; an ambiguous edge or
unproved intended removal yields `RECONCILIATION_REQUIRED`. Post-merge cleanup may reuse this same
primitive, but it is an optimization rather than the correctness boundary.

Source landing closes only the repository phase. Required rollout, activation, deployment, migration, runtime evidence, or operator acceptance remains owned by its actual post-merge gate. Terminal cleanup follows authoritative merged/closed/abandoned disposition and preserves the only recovery pointer when lineage is dirty or ambiguous.

## Invariants

- Restart reconstruction uses current authorities, not conversation memory or process-local queues.
- Visibility states do not grant mutation authority.
- Terminal response state never outruns the active role/procedure's already-required durable completion and readback.
- CI failures stay owned and explained; raw failure truth is preserved.
- Recovery is bounded, causal, and candidate-preserving.
- Cleanup never destroys the only recoverable unlanded work.
- `merged`, `deployed`, `activated`, and `complete` remain different facts.
- Existing audit/health machinery gardens stale architecture anchors and contradictions; documentation maintenance adds no scheduler or database.

## Current anchors

- [`../../agents/audit.md`](../../agents/audit.md)
- [`../../agents/development-workflow.md`](../../agents/development-workflow.md)

## Related documents

- [Authority and state](authority-and-state.md)
- [Lifecycle](lifecycle.md)
- [Work identity and concurrency](work-identity-and-concurrency.md)
- [Historical ADR 0003](decisions/0003-single-restartable-lifecycle-dispatcher.md)
- [ADR 0004](decisions/0004-phases-remain-distinct.md)
