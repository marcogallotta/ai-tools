# Review, certification, and Integration

## Read this when

Read this when changing Design Review, Code Review, exact-head certification, CI ownership, merge admission, or Integration mechanics.

## Scope

This document separates semantic judgment from executable evidence and final landing. Detailed verdict formats and commands stay in role contracts and runbooks.

## Current architecture

Design Review evaluates an exact frozen design generation when the governing workflow requires it. Code Review evaluates an exact GitHub PR head against the accepted task/design, handoff, architecture, and evidence. These are different candidates and neither role acquires Implementation or Integration authority.

Ordinary CI certifies the exact PR source head selected from the formal Review event, not a synthetic merge commit. [The shared gate predicate](../../../../scripts/pr_gate.py) combines current PR metadata, formal exact-head Review, status evidence, and applicable local certification into a deterministic Integration predicate. CI still pending does not delay semantic Review. CI failure authorizes a fix only after ownership is classified as PR-owned; unrelated/current-main/infrastructure failures remain visible without mutating the candidate.

Code-quality admission is author-first. When policy is enabled on either the exact base or candidate head, Implementation must persist exactly one acceptable `dish-code-quality-result-v1` for the current head before ready-for-Review state. Implementation finalization and lifecycle/Review discovery share that predicate; CI verification cannot create the missing author result. A successor head invalidates the result.

Final landing is a separately authorized local Integration action protected by a per-PR/head fence and fresh GitHub/Asana reads. Mechanical reconciliation that changes the head still requires an exact-head recheck; any semantic choice returns to Implementation and substantive Review.

## Invariants

- Material authorship and independent Review do not collapse into one actor for the same candidate.
- Formal Review is durable on the PR and bound to the exact reviewed identity.
- Test evidence proves only the boundary actually exercised.
- `PRE-INTEGRATION TESTS TO RUN` and `POST-MERGE GATES` preserve phase ownership.
- A green specialized workflow cannot substitute for required ordinary exact-head certification.
- Baseline-debt admission retains the failed raw evidence and requires the repository's typed proof boundary.
- Review never merges; Integration never invents semantic fixes.

## Current anchors

- [`../../agents/review.md`](../../agents/review.md)
- [`../../agents/integration.md`](../../agents/integration.md)
- [`../../testing.md`](../../testing.md)
- [`../testing-boundaries.md`](../testing-boundaries.md)
- [`../../../../scripts/pr_gate.py`](../../../../scripts/pr_gate.py)
- [`../../../../scripts/pr_lifecycle_local_integration.py`](../../../../scripts/pr_lifecycle_local_integration.py)

## Related documents

- [Lifecycle](lifecycle.md)
- [Authority and state](authority-and-state.md)
- [ADR 0004](decisions/0004-phases-remain-distinct.md)
