# ADR 0004: Review, certification, Integration, landing, and completion remain distinct

Status: Accepted

## Read this when

Read this when a proposal treats Review success, green CI, merge, deployment, activation, or task completion as equivalent.

## Context

These phases answer different questions and may use different authorities, evidence, hosts, and permissions. Collapsing them hides residual risk or moves a gate into the wrong phase.

## Decision

Design Review, Code Review, exact-head CI/certification, authorized Integration, source landing, and operational completion remain distinct facts. Post-merge acceptance stays post-merge unless its owning authority explicitly makes it a source-integration precondition.

## Consequences

A formal MERGE verdict starts Integration gate evaluation. A merged PR proves repository landing only. Durable state and human wording must identify remaining obligations without describing them as completed.

## Related documents

- [Lifecycle](../lifecycle.md)
- [Review, certification, and Integration](../review-certification-integration.md)
- [Recovery, observability, and completion](../recovery-observability-and-completion.md)
