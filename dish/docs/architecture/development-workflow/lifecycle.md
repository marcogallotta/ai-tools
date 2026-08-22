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

Before a material design or Implementation semantic commitment, the acting role resolves available
authoritative evidence and classifies any remaining understanding gap. A material unresolved
assumption cannot become architecture or code; only the affected semantic path pauses. Routine
bounded mechanics continue, and an irreducible Marco-only material fact is the only gap routed to
Marco. This is a correctness invariant, not a new lifecycle state or approval gate.

One exact governing Marco Intent Baseline/specification also bounds the implementation slice across
dispatch, authoring, and semantic Code Review. Working scope projections and a qualitative solution
envelope make drift visible but create no new intent authority. During authoring, material structural
departure is reconciled by shrinking, routing a genuinely necessary expansion through existing
design authority, or separating adjacent ownership before further expansion. Metrics are tripwires,
not gates, and trivial changes acquire no mandatory planning artifact.

Integration uses the exact reviewed candidate and performs only authorized mechanical reconciliation. A changed head returns to fresh Review. Source landing is distinct from deployment, migration, activation, or operator acceptance; a task becomes complete only after its actual residual obligations are done.

## Invariants

- Each semantic task retains its own commit/PR/task lineage even in an ordered stack.
- Review BLOCK fixes stay on the existing task/PR lineage.
- A successor head never inherits an older exact-head verdict silently.
- CI ownership is classified before a failing candidate is modified.
- Post-merge gates remain in their real phase and do not become source-merge blockers by proximity.
- Detailed GitHub substates are not duplicated as an Asana lifecycle system.
- Numeric confidence and green tests never substitute for the material understanding/evidence that
  the disputed semantic requirement actually needs.
- Scope projections and solution envelopes never replace the governing intent/specification, and an
  authoring trajectory check never replaces final semantic Review.

## Current anchors

- [`../../agents/implementation.md`](../../agents/implementation.md)
- [`../../agents/research.md`](../../agents/research.md)
- [`../../agents/scope-proportionality.md`](../../agents/scope-proportionality.md)
- [`../../agents/review.md`](../../agents/review.md)
- [`../../agents/integration.md`](../../agents/integration.md)
- [`../../../../scripts/pr_lifecycle.py`](../../../../scripts/pr_lifecycle.py)
- [`../../../../scripts/pr_gate.py`](../../../../scripts/pr_gate.py)

## Related documents

- [Review, certification, and Integration](review-certification-integration.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
- [ADR 0002](decisions/0002-durable-pr-exact-head-lifecycle.md)
- [ADR 0004](decisions/0004-phases-remain-distinct.md)
