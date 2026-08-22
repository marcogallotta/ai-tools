# Recovery, observability, and completion

## Read this when

Read this when changing restart recovery, ambiguous-effect handling, lifecycle visibility, cleanup, rollout tracking, or completion criteria.

## Scope

This document records recovery and terminal-state boundaries. Operational retry commands and dashboards belong to their runbooks.

## Current architecture

The development system recovers from durable GitHub, Asana, repository policy, and local claim/fence checkpoints. The lifecycle dispatcher has no authoritative queue database; on restart it reconstructs current state and routes only after fresh authority reads. Local Implementation and Integration use external durable state for exact lineage recovery, but those records never create assignment authority.

Ambiguous state-changing outcomes are reconciled before replay. A proven present effect is not repeated; a proven absent safe/idempotent effect may receive a bounded retry; unresolved ambiguity fails closed. Advisory activity signals help avoid duplicate work without becoming ownership or liveness proof.

Source landing closes only the repository phase. Required rollout, activation, deployment, migration, runtime evidence, or operator acceptance remains owned by its actual post-merge gate. Terminal cleanup follows authoritative merged/closed/abandoned disposition and preserves the only recovery pointer when lineage is dirty or ambiguous.

## Invariants

- Restart reconstruction uses current authorities, not conversation memory or process-local queues.
- Visibility states do not grant mutation authority.
- CI failures stay owned and explained; raw failure truth is preserved.
- Recovery is bounded, causal, and candidate-preserving.
- Cleanup never destroys the only recoverable unlanded work.
- `merged`, `deployed`, `activated`, and `complete` remain different facts.
- Existing audit/health machinery gardens stale architecture anchors and contradictions; documentation maintenance adds no scheduler or database.

## Current anchors

- [`../../../../scripts/pr_lifecycle.py`](../../../../scripts/pr_lifecycle.py)
- [`../../../../scripts/pr_lifecycle_local_integration.py`](../../../../scripts/pr_lifecycle_local_integration.py)
- [`../../agents/audit.md`](../../agents/audit.md)
- [`../../agents/development-workflow.md`](../../agents/development-workflow.md)
- [`../../../../ci/pr-lifecycle-dispatcher-runbook.md`](../../../../ci/pr-lifecycle-dispatcher-runbook.md)

## Related documents

- [Authority and state](authority-and-state.md)
- [Lifecycle](lifecycle.md)
- [Work identity and concurrency](work-identity-and-concurrency.md)
- [ADR 0003](decisions/0003-single-restartable-lifecycle-dispatcher.md)
- [ADR 0004](decisions/0004-phases-remain-distinct.md)
