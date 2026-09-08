# Development Workflow lifecycle

## Read this when

Read this when changing phase boundaries, lifecycle classification, handoff ordering, or completion semantics.

## Scope

This is the current lifecycle model, not an operating checklist. Role contracts and runbooks own action-level procedure.

## Current architecture

```mermaid
flowchart LR
    R[Research/design] --> DR[Design Review when required]
    DR --> I[Implementation]
    I --> P[Draft then review-ready PR]
    P --> CR[Independent exact-head Code Review]
    P --> CI[Exact-head CI/certification]
    CR --> G[Integration gate evaluation]
    CI --> G
    G --> IN[Authorized local Integration]
    IN --> M[Source landed]
    M --> A[Rollout/runtime acceptance when required]
    A --> D[Complete]
    CR -->|BLOCK| F[Same-lineage fix]
    F --> CR
```

Research/design becomes Implementation-ready only when its required decisions and pre-development review are durably satisfied. Implementation owns one task/branch lineage, publishes a real PR, finishes scoped evidence, and explicitly moves the PR from draft to review-ready. Review and ordinary exact-head CI may then proceed independently; pending CI is not a reason to delay semantic Review. A formal MERGE verdict begins gate evaluation rather than completing the task.

Integration uses the exact reviewed candidate and performs only authorized mechanical reconciliation. A changed head returns to fresh Review. Source landing is distinct from deployment, migration, activation, or operator acceptance; a task becomes complete only after its actual residual obligations are done.

### Explicit per-change lifecycle shortcuts

The normal lifecycle above remains the default. [`trivial-fast-track.md`](../../agents/trivial-fast-track.md)
defines Marco's explicit destination-driven exception for one bounded correction. The phrase itself
is route authority; agent-produced metadata must not become a pre-action operator gate.

- to main: bounded compare-and-set publication and readback happen immediately; PR, Review,
  pre-landing tests, and CI admission do not.
- to PR: publication and fresh exact-head Review remain, but ordinary CI is observed after landing
  rather than awaited before Integration.
- to testing: the primary local surface may temporarily carry an uncommitted reversible candidate;
  its exact tested delta is later captured into the PR route and the primary pre-state restored.
- agentic/generated documents are coherent only when canonical source and all owned projections are
  regenerated, regardless of route. Post-landing tests and cleanup stay off `main`.

## Invariants

- Each semantic task retains its own commit/PR/task lineage even in an ordered stack.
- Review BLOCK fixes stay on the existing task/PR lineage.
- A successor head never inherits an older exact-head verdict silently.
- CI ownership is classified before a failing candidate is modified.
- Post-merge gates remain in their real phase and do not become source-merge blockers by proximity.
- Detailed GitHub substates are not duplicated as an Asana lifecycle system.
- Per-change shortcut capability is exact and fail-closed; it never becomes a standing agent-owned waiver.

## Current anchors

- [`../../agents/implementation.md`](../../agents/implementation.md)
- [`../../agents/review.md`](../../agents/review.md)
- [`../../agents/integration.md`](../../agents/integration.md)
- [`../../agents/development-workflow.md`](../../agents/development-workflow.md)
- [`../../../../scripts/pr_gate.py`](../../../../scripts/pr_gate.py)

## Related documents

- [Review, certification, and Integration](review-certification-integration.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
- [ADR 0002](decisions/0002-durable-pr-exact-head-lifecycle.md)
- [ADR 0004](decisions/0004-phases-remain-distinct.md)
