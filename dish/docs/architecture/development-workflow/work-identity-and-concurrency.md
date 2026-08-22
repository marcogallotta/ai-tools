# Work identity and concurrency

## Read this when

Read this when changing task/branch/PR/worktree identity, ownership, takeover, cleanup, stacking, or dispatch concurrency.

## Scope

This document records identity and serialization boundaries. It does not impose a universal worker count or publication shape.

## Current architecture

A repository assignment is bound to repository, owning task, authorized branch, exact authoring base, and existing PR/head when present. A worktree lineage adds an immutable branch-incarnation identity and an exclusive local claim. A PR's current head is the semantic Review and certification identity. These identities overlap but are not interchangeable.

Concurrency is chosen from concrete landing relationships:

- independent work can proceed concurrently;
- parallel authoring with coordinated convergence is worthwhile only when useful work exceeds reconciliation/re-review cost;
- a true predecessor serializes downstream authoring;
- a coherent manual stack may keep separate task/commit/PR lineages while using each accepted head as the next base.

Dispatch waits require a verified causal edge: the exact upstream result, the exact downstream work it can materially change or invalidate, and current evidence for that interaction. Concrete-sounding timing, generic caution, fan-in counts, worker counts, or startup sequencing do not establish that edge. A proven independent classification persists until new material evidence establishes a causal, resource, authority, or dependency interaction.

Drain pressure is scoped to the actual bottleneck. The Coordinator constructs the full eligible high-priority frontier before applying waits; unrelated authoring, PR, CI, Review, or Integration fan-in does not remove independent Research or Design Review from that frontier.

## Invariants

- One semantic Implementation owner writes a branch lineage at a time.
- Local agents use the repository-owned worktree/claim lifecycle; local refs are caches.
- Branch names managed by that lifecycle are single-use after terminal cleanup.
- Takeover requires explicit handoff and compare-and-set against current claim identity; silence or age is not owner death.
- Advisory PR leases improve visibility but never grant ownership.
- Unrelated movement of the target branch does not silently replace an established authoring base.
- Stack propagation preserves later completed work when an earlier accepted correction must be down-merged.
- Every omitted executable P-CRITICAL/P0 action has a named concrete current causal, resource, authority, or dependency reason.
- Operator dispatch classes are unambiguous: send now, wait for a verified edge/result, or request an exact Marco decision; `SEND NEXT` does not encode a dependency.

## Current anchors

- [`../../agents/templates/implementation-handoff.md`](../../agents/templates/implementation-handoff.md)
- [`../../../../tools/agent-worktree`](../../../../tools/agent-worktree)
- [`../../../../tools/agent-worktree-handoff.md`](../../../../tools/agent-worktree-handoff.md)
- [`../../agents/coordinator.md`](../../agents/coordinator.md)

## Related documents

- [Lifecycle](lifecycle.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
- [ADR 0002](decisions/0002-durable-pr-exact-head-lifecycle.md)
