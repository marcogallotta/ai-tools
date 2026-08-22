# ADR 0001: Development authority is split by fact

Status: Accepted

## Read this when

Read this when proposing to move or duplicate GitHub, Asana, repository-policy, or runtime authority.

## Context

Reliable recovery requires a replacement agent to know which system owns each fact. Treating a convenient cache, task comment, local checkout, or runtime assumption as global truth creates contradictory writers.

## Decision

GitHub owns repository source/history, PR heads, formal reviews, and CI. The live owning Asana task/project owns development orchestration and accepted design/decision state. Current repository contracts own role/process policy. Direct environment evidence owns deployed runtime facts. Projections may combine these facts but do not replace them.

## Consequences

Cross-surface contradictions require reconciliation against the owning authority. Authenticated-account metadata and conversation memory are provenance/context, not independent human-decision or lifecycle authority.

## Related documents

- [Authority and state](../authority-and-state.md)
- [System context](../system-context.md)
