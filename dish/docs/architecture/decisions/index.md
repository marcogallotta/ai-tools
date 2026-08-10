# Architecture decisions

## Read this when

Read this when a change appears to reopen a settled high-risk product/safety choice.

## Scope

This index contains the small set of decisions that are intentionally stronger than descriptive current architecture.

## Authoritative implementation

Implementation anchors live in the related domain documents; ADRs should not freeze module topology.

## Actors, processes, and stores

The decisions apply to the current service, migration target, dark-launch machinery, and cutover flow as relevant.

## Authority and data ownership

| Decision | Status |
|---|---|
| [Dark launch does not transfer authority](0001-dark-launch-does-not-transfer-authority.md) | Accepted |
| [Request identity is permanent](0002-request-identity-is-permanent.md) | Accepted |
| [Approval and application are separate](0003-approval-and-application-are-separate.md) | Accepted |
| [Shadow-origin work never projects](0004-shadow-origin-never-projects.md) | Accepted |
| [Cutover evidence is bounded](0005-cutover-evidence-is-bounded.md) | Accepted |
| [Cutover bar matches actual operating context](0006-cutover-bar-matches-operating-context.md) | Accepted |

## Invariants

Only promote a rule to an ADR when it is a deliberate durable decision, not because current code happens to work that way.

## Process and transaction boundaries

ADRs constrain semantics, not commit topology or exact transaction-owner modules unless that is itself the accepted decision.

## Normal flow

Consult an ADR when a change would alter its semantic guarantee; otherwise use the domain architecture documents.

## Failure, replay, recovery, and concurrency

A stale implementation should be fixed or the ADR deliberately superseded. An ADR should not be used to preserve accidental implementation behavior.

## Change routing

New ADRs require a real consequential decision with rationale/provenance. Coding conventions, test procedure, module placement, and temporary migration mechanics do not belong here.

## Proving tests

Behavioral evidence is referenced from the related domain architecture documents.

## Current debt and temporary compatibility

No attempt is made to reconstruct every historical decision. This list is intentionally small.

## Related documents

- [Architecture index](../index.md)
- [Extension rules](../extension-rules.md)
