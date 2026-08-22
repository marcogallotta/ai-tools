# ADR 0002: Code changes use durable PR and exact-head lineage

Status: Accepted

## Read this when

Read this when proposing patch-only handoff, branch reuse, PR-number-only Review, or silent verdict transfer to a new head.

## Context

Agents and sessions are replaceable. A reviewable candidate must survive conversation loss and must not be confused with an older/newer version on the same branch or PR.

## Decision

Normal code work uses an owned branch and semantic commit, a real GitHub PR, independent Review of the exact current PR head, and Integration of that reviewed identity. Task/branch/worktree lineage remains durable and separate for each semantic work item, including ordered stacks.

## Consequences

Any semantic head movement requires fresh substantive Review; genuinely mechanical movement requires an explicit exact-head mechanical recheck. Fixes remain on the existing task/PR lineage unless current authority explicitly replaces it.

## Related documents

- [Lifecycle](../lifecycle.md)
- [Work identity and concurrency](../work-identity-and-concurrency.md)
