# Work identity and concurrency

## Read this when

Read this when changing task/branch/PR/worktree identity, ownership, takeover, cleanup, stacking, or dispatch concurrency.

## Scope

This document records identity and serialization boundaries. It does not impose a universal worker count or publication shape.

## Current architecture

A repository assignment is bound to repository, owning task, authorized branch, exact authoring base, and existing PR/head when present. A worktree lineage adds an immutable branch-incarnation identity and an exclusive local claim. A PR's current head is the semantic Review and certification identity. These identities overlap but are not interchangeable.

An explicit fast-track destination is immediate action authority for that route; it is not a request to manufacture a pre-action capability record. Fast-track to main intentionally uses the primary checkout and commits and pushes the exact requested change before tests or cleanup. Fast-track to testing intentionally uses the active local main/testing surface without committing. Fast-track to PR uses an owned branch and PR, retains independent exact-head Review, and moves ordinary CI outside merge admission. The action itself produces the durable evidence needed for readback and any post-landing follow-up.

Concurrency is chosen from concrete landing relationships:

- independent work can proceed concurrently;
- parallel authoring with coordinated convergence is worthwhile only when useful work exceeds reconciliation/re-review cost;
- a true predecessor serializes downstream authoring;
- a coherent manual stack may keep separate task/commit/PR lineages while using each accepted head as the next base.

## Invariants

- One semantic Implementation owner writes a branch lineage at a time.
- Local agents use the repository-owned worktree/claim lifecycle; local refs are caches.
- Start/adopt persist an exact PREPARED creation attempt before branch/worktree effects; exact retries finish the same lineage, while moved or ambiguous partial effects fail closed.
- Terminal cleanup takes the existing branch/PR claim locks before its task lock and holds them through exact-head deletion so a live writer cannot race cleanup.
- Branch names managed by that lifecycle are single-use after terminal cleanup.
- Takeover requires explicit handoff and compare-and-set against current claim identity; silence or age is not owner death.
- Advisory PR leases improve visibility but never grant ownership.
- A verified exact-head Review BLOCK may replace prior assignment authority only
  inside the same task, branch, authoring-base, PR, and lineage after publication
  has durably closed the prior semantic writer claim.
- When Integration has conflict-free refreshed that closed successor onto the exact
  current target head, a later exact-head Review BLOCK may advance the same lineage
  only after the tool verifies the two exact merge parents and mechanically
  reproduces the merge tree. The current claim generation, clean preserved
  worktree head, and all stable assignment fields remain compare-and-set fences.
- Local first claim consumes one current exact Implementation handoff from the owning task; Ready,
  a local identity file, or a claim file cannot independently authorize repository mutation.
- Worktree admission accepts exactly one owning membership from the registered V2 project
  authority, validates that project's live V2 name and complete lifecycle signature, and refreshes
  that authority at every material writer boundary.
- Unrelated movement of the target branch does not silently replace an established authoring base.
- Stack propagation preserves later completed work when an earlier accepted correction must be down-merged.
- The destination dominates every spelling or generic label. An explicit destination does not trigger a confirmation loop; only a materially wrong-looking route gets one concise warning. A bare fast-track request requires the agent to recommend one route and ask once.
- Fast-track to main and fast-track to testing are explicit exceptions to the ordinary shared-primary-checkout prohibition for the exact requested change. Their post-fast-track tests, cleanup, and fixes move to an owned branch/PR rather than accumulating on local or remote main.

## Current anchors

- [`../../agents/templates/implementation-handoff.md`](../../agents/templates/implementation-handoff.md)
- [`../../../../tools/agent-worktree`](../../../../tools/agent-worktree)
- [`../../../../tools/agent-worktree-handoff.md`](../../../../tools/agent-worktree-handoff.md)
- [`../../agents/coordinator.md`](../../agents/coordinator.md)

## Related documents

- [Lifecycle](lifecycle.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
- [ADR 0002](decisions/0002-durable-pr-exact-head-lifecycle.md)
