# Architecture decisions

## Read this when

Read this when a change appears to reopen a settled authority, replay, Human Review, shadow-effect, or cutover-evidence decision.

## Scope

This index lists small current decision records. It is not a historical ADR reconstruction and does not replace domain architecture documents.

## Authoritative implementation

Each decision links exact implementation and proving tests. A decision remains accepted only while those authorities remain current.

## Actors, processes, and stores

The decisions apply to the current service, PostgreSQL target, dark-launch worker, and release/cutover tooling described by the linked documents.

## Authority and data ownership

| Decision | Status | Primary owner |
|---|---|---|
| [ADR-0001: Dark launch does not transfer authority](0001-dark-launch-does-not-transfer-authority.md) | Accepted | Current service plus dark-launch capture/target evidence |
| [ADR-0002: Request identity is permanent](0002-request-identity-is-permanent.md) | Accepted | Request replay authority |
| [ADR-0003: Approval and application are separate](0003-approval-and-application-are-separate.md) | Accepted | Semantic proposal/Human Review workflow |
| [ADR-0004: Shadow-origin work never projects](0004-shadow-origin-never-projects.md) | Accepted | Projection origin/effect isolation |
| [ADR-0005: Cutover evidence is bounded](0005-cutover-evidence-is-bounded.md) | Accepted | Release/cutover evidence authority |

## Invariants

Do not create an ADR for speculative or merely historical behavior. Update or supersede a record only with a code/product decision that actually changes the settled boundary.

## Process and transaction boundaries

Decision records point to the transaction/process owners; they do not introduce new boundaries.

## Normal flow

Consult the relevant ADR, then read its related domain architecture document and authoritative code before implementation.

## Failure, replay, recovery, and concurrency

A decision record that conflicts with current code/tests is stale and must be corrected; it cannot override runtime authority.

## Change routing

Add a new ADR only for a settled choice that agents repeatedly risk reopening and whose rationale/consequences are supported by repository authority or approved product decisions.

## Proving tests

`tests/test_architecture_knowledge_base.py` verifies that all decision records are indexed and linked.

## Current debt and temporary compatibility

No historical ADR series was reconstructed. These records capture only currently settled, high-risk boundaries.

## Related documents

- [Canonical architecture index](../index.md)
- [Extension rules](../extension-rules.md)
